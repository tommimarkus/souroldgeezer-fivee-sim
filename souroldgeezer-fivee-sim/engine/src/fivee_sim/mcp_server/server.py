"""MCP stdio server: a thin adapter over the engine.

Every tool validates its input, calls the kernel or the encounter model, and
serialises the result. No rules logic belongs in this file — if a behaviour needs
deciding, it is decided in ``kernel`` or ``model`` where the tests can reach it.

Two conventions worth knowing:

* Every tool that consumes randomness accepts an optional ``seed`` and **always
  reports the seed it used**. Omitting one does not make a result irreproducible;
  it makes the engine choose a seed and tell you, so any roll can be replayed.
* Encounter state lives in this process, keyed by an id. ``encounter_state`` is
  the authoritative view — narration should follow it rather than memory.

Anything written to stdout other than protocol traffic corrupts the stream, so
diagnostics go to stderr.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from random import Random
from typing import Any

from mcp.server.mcpserver import MCPServer

from .. import __version__
from ..analytics.montecarlo import simulate_dpr as _simulate_dpr
from ..analytics.montecarlo import simulate_rounds as _simulate_rounds
from ..content import (
    BUILTIN_ENV,
    CONTENT_ENV,
    BuiltinMode,
    ContentError,
    ContentRegistry,
    builtin_mode,
    builtin_registry,
    environment_paths,
    load_packs,
)
from ..content import validate as _validate_content
from ..data import DataError, make_creature
from ..kernel.actions import AttackKind, RiderExpiry
from ..kernel.dice import Advantage, Dice, roll_d20, roll_dice
from ..kernel.grid import (
    DiagonalRule,
    Point,
    Square,
    TerrainEffect,
    UnknownTerrain,
    as_point,
    to_square,
)
from ..kernel.rules import Ability, DamageType, make_d20_test
from ..maps import MapDocument, as_payload, to_grid
from ..maps import serialize as _serialize_map
from ..model.battlemap import BattleMap, MapFeature
from ..model.creature import AttackOption, Creature
from ..model.encounter import Action, ActionKind, Encounter, EncounterError
from ..service import maps as _map_service
from ..service.common import resolve_seed, sha256_of, slugify

INSTRUCTIONS = """\
A 5E-compatible combat engine. The engine owns the fight: hit points, initiative
order, conditions, and dice are computed here, so read encounter_state as
authoritative and narrate from it rather than tracking state yourself.

Content is configurable. The bundled SRD 5.2 slice loads by default, and a campaign
may add its own creatures, spells, conditions, terrain, and items as content packs —
or run on its own material alone. Call content_status to see what is actually loaded before
telling anyone what is available.

Bundled rules content comes from SRD 5.2 under CC-BY-4.0; see the plugin's NOTICE.
"""

server: MCPServer = MCPServer(
    name="souroldgeezer-fivee-sim",
    version=__version__,
    instructions=INSTRUCTIONS,
)


@dataclass(slots=True)
class _Session:
    encounter: Encounter
    rng: Random
    seed: int
    #: Which content generation this fight was built against. An encounter keeps
    #: resolving under the content it started with, so this is how a later
    #: reconfiguration becomes visible rather than mysterious.
    content_generation: int = 0
    #: The map session this fight was built from, if any, with the generation and
    #: document hash it captured — the same staleness idiom as content: an edit
    #: never reaches into a fight, so the divergence is reported instead.
    map_id: str | None = None
    map_generation: int = 0
    map_sha256: str = ""


@dataclass(slots=True)
class _MapSession:
    """One loaded map. The document is frozen; edits replace it and bump the
    generation, which is how an encounter built from it can tell it moved on."""

    document: MapDocument
    generation: int = 1
    path: str | None = None


@dataclass(slots=True)
class _Content:
    """The active registry, replaced wholesale rather than mutated."""

    registry: ContentRegistry
    generation: int = 1
    configured: tuple[str, ...] = ()
    #: Set when content named by the environment could not be loaded at start-up.
    startup_error: str = ""


_SESSIONS: dict[str, _Session] = {}
_NEXT_ID = 0
_MAPS: dict[str, _MapSession] = {}
_NEXT_MAP_ID = 0
_CONTENT: _Content | None = None


class ToolError(ValueError):
    """Bad tool input, reported to the caller rather than crashing the server."""


def _content() -> _Content:
    """The active content, loaded from the environment on first use.

    A pack the environment names but that will not load must not take the server
    down with it: the process would fail its handshake and the user would get no
    tools at all, with the reason invisible. Instead the built-in slice loads, the
    failure goes to stderr, and ``content_status`` reports it — so the session works
    and the problem is still discoverable.
    """
    global _CONTENT
    if _CONTENT is None:
        try:
            _CONTENT = _Content(registry=load_packs(builtin=builtin_mode()))
        except ContentError as error:
            print(f"fivee-sim: falling back to bundled content: {error}", file=sys.stderr)
            _CONTENT = _Content(registry=builtin_registry(), startup_error=str(error))
    return _CONTENT


def _registry() -> ContentRegistry:
    return _content().registry


def _mode(value: str | None, *, default: BuiltinMode) -> BuiltinMode:
    if value is None:
        return default
    try:
        return BuiltinMode(value.strip().casefold())
    except ValueError as error:
        allowed = ", ".join(item.value for item in BuiltinMode)
        raise ToolError(f"builtin must be one of: {allowed}") from error


def _new_encounter_id() -> str:
    global _NEXT_ID
    _NEXT_ID += 1
    return f"enc-{_NEXT_ID}"


# The seed convention lives in the service layer now; the alias keeps every
# existing tool body reading as it always has.
_resolve_seed = resolve_seed


def _advantage(value: str | None) -> Advantage:
    try:
        return Advantage(value or "none")
    except ValueError as error:
        allowed = ", ".join(state.value for state in Advantage)
        raise ToolError(f"advantage must be one of: {allowed}") from error


def _movement_rule(value: str) -> DiagonalRule:
    try:
        return DiagonalRule(value)
    except ValueError as error:
        allowed = ", ".join(rule.value for rule in DiagonalRule)
        raise ToolError(f"movement_rule must be one of: {allowed}") from error


def _point(value: int | list[int], what: str) -> Point | int:
    """Accept a position as feet along the x-axis or as an ``[x, y]`` pair."""
    if isinstance(value, int):
        return value
    if (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(part, int) and not isinstance(part, bool) for part in value)
    ):
        return (value[0], value[1])
    raise ToolError(f"{what} must be feet along the x-axis or an [x, y] pair of feet")


def _session(encounter_id: str) -> _Session:
    session = _SESSIONS.get(encounter_id)
    if session is None:
        known = ", ".join(sorted(_SESSIONS)) or "none"
        raise ToolError(f"unknown encounter {encounter_id!r}; active: {known}")
    return session


def _new_map_id() -> str:
    global _NEXT_MAP_ID
    _NEXT_MAP_ID += 1
    return f"map-{_NEXT_MAP_ID}"


def _map_session(map_id: str) -> _MapSession:
    session = _MAPS.get(map_id)
    if session is None:
        known = ", ".join(sorted(_MAPS)) or "none"
        raise ToolError(f"unknown map {map_id!r}; active: {known}")
    return session


def _map_summary(document: MapDocument) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in document.tiles:
        for char in row:
            kind = document.legend[char]
            counts[kind] = counts.get(kind, 0) + 1
    return {
        "width": document.grid.width,
        "height": document.grid.height,
        "features": len(document.features),
        "terrain_counts": {kind: counts[kind] for kind in sorted(counts)},
    }


def _map_source_of(session: _Session) -> dict[str, Any] | None:
    """How the fight's map relates to the live map session, or ``None``.

    ``stale`` flips when the map has been edited or reloaded since the fight
    captured it — the fight keeps resolving on what it captured, and this is
    the divergence made visible, exactly as content generations work.
    """
    if session.map_id is None:
        return None
    live = _MAPS.get(session.map_id)
    current = live.generation if live is not None else None
    return {
        "map_id": session.map_id,
        "generation": session.map_generation,
        "current_generation": current,
        "stale": current != session.map_generation,
    }


def _resolve_battle_map(
    map_spec: dict[str, Any] | None, map_id: str | None
) -> tuple[BattleMap | None, dict[str, Any] | None]:
    """The battle map a tool call names — inline spec or loaded session.

    A session-backed map also yields the ``map_source`` capture: which map,
    which generation, and the hash of the exact document the fight is on.
    """
    if map_spec is not None and map_id is not None:
        raise ToolError("give 'map' (an inline spec) or 'map_id' (a loaded map), not both")
    if map_spec is not None:
        return _battle_map_from_spec(map_spec), None
    if map_id is not None:
        session = _map_session(map_id)
        return to_grid(session.document), {
            "map_id": map_id,
            "map_generation": session.generation,
            "map_sha256": sha256_of(_serialize_map(session.document)),
        }
    return None, None


def _attack_from_spec(spec: dict[str, Any]) -> AttackOption:
    bonus_type = spec.get("bonus_damage_type")
    save_ability = spec.get("on_hit_save_ability")
    try:
        return AttackOption(
            name=str(spec["name"]),
            attack_bonus=int(spec["attack_bonus"]),
            damage=Dice.parse(str(spec["damage"])),
            damage_type=DamageType(spec["damage_type"]),
            kind=AttackKind(spec.get("kind", "melee")),
            reach=int(spec.get("reach", 5)),
            normal_range=int(spec.get("normal_range", 0)),
            long_range=int(spec.get("long_range", 0)),
            bonus_damage=(
                Dice.parse(str(spec["bonus_damage"]))
                if spec.get("bonus_damage") is not None else None
            ),
            bonus_damage_type=(
                DamageType(bonus_type) if bonus_type is not None else None
            ),
            advantage_bonus_damage=(
                Dice.parse(str(spec["advantage_bonus_damage"]))
                if spec.get("advantage_bonus_damage") is not None else None
            ),
            on_hit_condition=(
                str(spec["on_hit_condition"])
                if spec.get("on_hit_condition") is not None else None
            ),
            on_hit_save_ability=(
                Ability(save_ability) if save_ability is not None else None
            ),
            on_hit_save_dc=int(spec.get("on_hit_save_dc", 0)),
            on_hit_expiry=RiderExpiry(spec.get("on_hit_expiry", "none")),
            provenance=str(spec.get("provenance", "caller-supplied")),
        )
    except KeyError as error:
        raise ToolError(f"attack spec is missing {error.args[0]!r}") from error
    except ValueError as error:
        raise ToolError(f"attack spec is invalid: {error}") from error


def _creature_from_spec(spec: dict[str, Any], registry: ContentRegistry) -> Creature:
    """Build a combatant from a loaded stat block or an explicit description.

    ``monster`` and ``creature`` are accepted interchangeably; the stat block is
    looked up in ``registry``, so which names resolve depends on what is loaded.
    """
    named = spec.get("creature", spec.get("monster"))
    if named is not None:
        try:
            return make_creature(
                str(named),
                registry=registry,
                label=spec.get("label"),
                team=spec.get("team"),
                position=_point(spec.get("position", 0), "position"),
            )
        except DataError as error:
            raise ToolError(str(error)) from error
    try:
        return Creature(
            name=str(spec["name"]),
            team=str(spec["team"]),
            ac=int(spec["ac"]),
            max_hp=int(spec["max_hp"]),
            hp=int(spec.get("hp", -1)),
            speed=int(spec.get("speed", 30)),
            abilities={
                Ability(key): int(value)
                for key, value in spec.get("abilities", {}).items()
            },
            save_bonuses={
                Ability(key): int(value)
                for key, value in spec.get("save_bonuses", {}).items()
            },
            attacks=tuple(_attack_from_spec(entry) for entry in spec.get("attacks", [])),
            attacks_per_action=int(spec.get("attacks_per_action", 1)),
            spells=tuple(str(name) for name in spec.get("spells", [])),
            spell_slots={int(k): int(v) for k, v in spec.get("spell_slots", {}).items()},
            spell_save_dc=int(spec.get("spell_save_dc", 10)),
            spell_attack_bonus=int(spec.get("spell_attack_bonus", 0)),
            resistances=frozenset(
                DamageType(entry) for entry in spec.get("resistances", [])
            ),
            immunities=frozenset(DamageType(entry) for entry in spec.get("immunities", [])),
            vulnerabilities=frozenset(
                DamageType(entry) for entry in spec.get("vulnerabilities", [])
            ),
            items={str(k): int(v) for k, v in spec.get("items", {}).items()},
            conditions={str(entry) for entry in spec.get("conditions", [])},
            condition_effects=registry.condition_effects,
            position=_point(spec.get("position", 0), "position"),
            provenance=str(spec.get("provenance", "caller-supplied")),
        )
    except KeyError as error:
        raise ToolError(
            f"combatant spec is missing {error.args[0]!r}; give either "
            f"{{'creature': '<loaded name>'}} or name/team/ac/max_hp"
        ) from error


def _combatants(specs: list[dict[str, Any]], registry: ContentRegistry) -> list[Creature]:
    if len(specs) < 2:
        raise ToolError("an encounter needs at least two combatants")
    return [_creature_from_spec(spec, registry) for spec in specs]


_MAP_KEYS = frozenset({
    "name", "width", "height", "default_terrain", "rows", "legend", "terrain",
    "features",
})
_FEATURE_KEYS = frozenset({
    "name", "square", "kind", "initially_open", "closed_terrain", "open_terrain",
})
#: An inline map is authored by hand or by a model, not generated; this bound only
#: exists so a malformed spec fails with a size complaint instead of an allocation.
MAX_MAP_SQUARES = 512


def _square(value: Any, what: str, width: int, height: int) -> tuple[int, int]:
    if not (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(part, int) and not isinstance(part, bool) for part in value)
    ):
        raise ToolError(f"{what} must be an [x, y] pair of squares")
    x, y = value
    if not (0 <= x < width and 0 <= y < height):
        raise ToolError(
            f"{what} is [{x}, {y}], outside the {width}x{height} map"
        )
    return (x, y)


def _map_dimension(spec: dict[str, Any], key: str) -> int:
    value = spec.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"map {key} is required and must be a whole number of squares")
    if not 1 <= value <= MAX_MAP_SQUARES:
        raise ToolError(f"map {key} must be between 1 and {MAX_MAP_SQUARES}, got {value}")
    return value


def _battle_map_from_spec(spec: dict[str, Any]) -> BattleMap:
    """Build a :class:`BattleMap` from the inline tool spec, refusing precisely.

    Terrain is authored either as ``rows`` of characters with a ``legend`` — the
    form a person or a model writes by hand — or as a ``terrain`` list of
    ``{"kind", "squares"}`` entries. Terrain *kinds* are not resolved here: the
    encounter validates them against the content it captured, so a pack-defined
    kind works and an unknown one is refused with the loaded list.
    """
    for key in sorted(set(spec) - _MAP_KEYS):
        raise ToolError(
            f"unknown map key {key!r}. Valid keys: {', '.join(sorted(_MAP_KEYS))}"
        )
    width = _map_dimension(spec, "width")
    height = _map_dimension(spec, "height")
    default_terrain = str(spec.get("default_terrain", "normal"))
    terrain: dict[tuple[int, int], str] = {}

    rows = spec.get("rows")
    entries = spec.get("terrain")
    if rows is not None and entries is not None:
        raise ToolError("give 'rows' with a 'legend', or a 'terrain' list — not both")
    if rows is not None:
        legend = spec.get("legend")
        if not isinstance(legend, dict) or not all(
            isinstance(key, str) and len(key) == 1 and isinstance(value, str)
            for key, value in legend.items()
        ):
            raise ToolError(
                "'rows' needs a 'legend' object mapping single characters to "
                "terrain kinds, such as {\"#\": \"wall\", \".\": \"normal\"}"
            )
        if not isinstance(rows, list) or not all(isinstance(row, str) for row in rows):
            raise ToolError("'rows' must be a list of strings, one per map row")
        if len(rows) != height:
            raise ToolError(f"'rows' has {len(rows)} rows; the map is {height} high")
        for y, row in enumerate(rows):
            if len(row) != width:
                raise ToolError(
                    f"row {y} is {len(row)} characters; the map is {width} wide"
                )
            for x, char in enumerate(row):
                kind = legend.get(char)
                if kind is None:
                    raise ToolError(
                        f"row {y} column {x} uses {char!r}, which the legend does "
                        f"not define"
                    )
                if kind != default_terrain:
                    terrain[(x, y)] = kind
    elif entries is not None:
        if not isinstance(entries, list):
            raise ToolError("'terrain' must be a list of {kind, squares} entries")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or set(entry) != {"kind", "squares"}:
                raise ToolError(
                    f"terrain entry #{index} must be {{\"kind\": ..., \"squares\": "
                    f"[[x, y], ...]}}"
                )
            kind = entry["kind"]
            if not isinstance(kind, str):
                raise ToolError(f"terrain entry #{index} kind must be a terrain name")
            squares = entry["squares"]
            if not isinstance(squares, list):
                raise ToolError(f"terrain entry #{index} squares must be a list")
            for value in squares:
                terrain[_square(value, f"terrain entry #{index} square", width, height)] = kind

    features: dict[str, MapFeature] = {}
    raw_features = spec.get("features", [])
    if not isinstance(raw_features, list):
        raise ToolError("'features' must be a list of feature objects")
    for index, entry in enumerate(raw_features):
        if not isinstance(entry, dict):
            raise ToolError(f"feature #{index} must be an object")
        for key in sorted(set(entry) - _FEATURE_KEYS):
            raise ToolError(
                f"feature #{index} has unknown key {key!r}. Valid keys: "
                f"{', '.join(sorted(_FEATURE_KEYS))}"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ToolError(f"feature #{index} needs a non-empty 'name'")
        if name in features:
            raise ToolError(f"two features are named {name!r}; names must be unique")
        initially_open = entry.get("initially_open", False)
        if not isinstance(initially_open, bool):
            raise ToolError(f"feature {name!r} initially_open must be true or false")
        features[name] = MapFeature(
            name=name,
            square=_square(entry.get("square"), f"feature {name!r} square",
                           width, height),
            kind=str(entry.get("kind", "door")),
            closed_terrain=str(entry.get("closed_terrain", "door-closed")),
            open_terrain=str(entry.get("open_terrain", "door-open")),
            initially_open=initially_open,
        )

    return BattleMap(
        name=str(spec.get("name", "battle map")),
        width=width,
        height=height,
        default_terrain=default_terrain,
        terrain=terrain,
        features=features,
    )


def _new_encounter(
    combatants: list[Creature],
    rng: Random,
    registry: ContentRegistry,
    *,
    movement_rule: DiagonalRule = DiagonalRule.FIVE_FIVE_FIVE,
    battle_map: BattleMap | None = None,
) -> Encounter:
    """Build an encounter bound to ``registry``'s tables, captured by value."""
    return Encounter(
        combatants,
        rng,
        spellbook=registry.spells,
        items=registry.items,
        condition_effects=registry.condition_effects,
        movement_rule=movement_rule,
        battle_map=battle_map,
        terrain_effects=registry.terrain_effects,
    )


# --- primitives ------------------------------------------------------------
@server.tool()
def roll(expression: str, advantage: str = "none", seed: int | None = None) -> dict[str, Any]:
    """Roll a dice expression such as "2d6+3" or "d20", optionally with advantage.

    Advantage and disadvantage apply only to a single d20; they are ignored for
    other expressions because the rules attach them to d20 tests.
    """
    used = _resolve_seed(seed)
    rng = Random(used)
    dice = Dice.parse(expression)
    state = _advantage(advantage)
    if dice.count == 1 and dice.faces == 20 and state is not Advantage.NONE:
        d20 = roll_d20(rng, state)
        return {
            "expression": str(dice),
            "seed": used,
            "advantage": state.value,
            "natural": d20.natural,
            "rolls": list(d20.rolls),
            "total": d20.natural + dice.modifier,
            "detail": d20.describe(),
        }
    result = roll_dice(dice, rng)
    return {
        "expression": str(dice),
        "seed": used,
        "advantage": Advantage.NONE.value,
        "rolls": list(result.rolls),
        "total": result.total,
        "detail": result.describe(),
    }


@server.tool()
def check(
    modifier: int,
    dc: int,
    advantage: str = "none",
    seed: int | None = None,
) -> dict[str, Any]:
    """Make an ability check against a DC."""
    used = _resolve_seed(seed)
    test = make_d20_test(Random(used), modifier=modifier, dc=dc, advantage=_advantage(advantage))
    return {
        "seed": used,
        "natural": test.roll.natural,
        "total": test.total,
        "dc": dc,
        "success": test.success,
        "detail": test.describe(),
    }


@server.tool()
def save(
    modifier: int,
    dc: int,
    advantage: str = "none",
    auto_fail: bool = False,
    seed: int | None = None,
) -> dict[str, Any]:
    """Make a saving throw. ``auto_fail`` covers conditions that forfeit the save."""
    used = _resolve_seed(seed)
    test = make_d20_test(
        Random(used),
        modifier=modifier,
        dc=dc,
        advantage=_advantage(advantage),
        auto_fail=auto_fail,
    )
    return {
        "seed": used,
        "natural": test.roll.natural,
        "total": test.total,
        "dc": dc,
        "success": test.success,
        "auto_failed": test.auto_failed,
        "detail": test.describe(),
    }


def _condition_entry(registry: ContentRegistry, name: str) -> dict[str, Any]:
    effect = registry.condition_effects[name]
    record = registry.condition_records.get(name, {})
    return {
        "kind": "condition",
        "name": name,
        "effects": {
            flag: getattr(effect, flag)
            for flag in effect.__dataclass_fields__
            if getattr(effect, flag)
        } or {"note": "no combat-roll consequences"},
        "description": str(record.get("description", "")),
        "source": registry.source_of("conditions", name),
        "provenance": str(record.get("provenance", "SRD 5.2")),
        "unmodelled": list(record.get("unmodelled", [])),
    }


def _spell_entry(registry: ContentRegistry, name: str) -> dict[str, Any]:
    spell = registry.spells[name]
    record = registry.spell_records.get(name, {})
    return {
        "kind": "spell",
        "name": spell.name,
        "level": spell.level,
        "school": spell.school,
        "save": spell.save_ability.value if spell.save_ability else None,
        "attack_roll": spell.requires_attack_roll,
        "damage": str(spell.damage) if spell.damage else None,
        "damage_type": spell.damage_type.value if spell.damage_type else None,
        "half_on_save": spell.half_on_save,
        "upcast_damage": str(spell.upcast_damage) if spell.upcast_damage else None,
        "shape": spell.effective_shape.value,
        "radius": spell.radius,
        "length": spell.length,
        "size": spell.size,
        "range_feet": spell.range_feet,
        "condition": spell.condition,
        "concentration": spell.concentration,
        "source": registry.source_of("spells", name),
        "provenance": spell.provenance,
        "unmodelled": list(record.get("unmodelled", [])),
    }


def _item_entry(registry: ContentRegistry, name: str) -> dict[str, Any]:
    effect = registry.items[name]
    record = registry.item_records.get(name, {})
    return {
        "kind": "item",
        "name": name,
        "use": {
            "heal": str(effect.heal) if effect.heal else None,
            "damage": str(effect.damage) if effect.damage else None,
            "damage_type": effect.damage_type.value if effect.damage_type else None,
            "save_ability": effect.save_ability.value if effect.save_ability else None,
            "save_dc": effect.save_dc or None,
            "half_on_save": effect.half_on_save,
            "condition": effect.condition,
        },
        "description": effect.description,
        "source": registry.source_of("items", name),
        "provenance": effect.provenance,
        "unmodelled": list(record.get("unmodelled", [])),
    }


def _terrain_entry(registry: ContentRegistry, name: str) -> dict[str, Any]:
    effect = registry.terrain_effects[name]
    record = registry.terrain_records.get(name, {})
    defaults = TerrainEffect()
    return {
        "kind": "terrain",
        "name": name,
        "effects": {
            flag: getattr(effect, flag)
            for flag in effect.__dataclass_fields__
            if getattr(effect, flag) != getattr(defaults, flag)
        } or {"note": "ordinary ground; no movement or sight consequences"},
        "description": str(record.get("description", "")),
        "source": registry.source_of("terrain", name),
        "provenance": str(record.get("provenance", "engine policy")),
        "unmodelled": list(record.get("unmodelled", [])),
    }


def _creature_entry(registry: ContentRegistry, name: str) -> dict[str, Any]:
    record = registry.creatures[name]
    entry: dict[str, Any] = {"kind": "creature", **record}
    entry["source"] = registry.source_of("creatures", name)
    # ``unmodelled`` is present even when empty. The skill tells Claude to check it
    # before promising a trait will fire, and that instruction has to stay true for a
    # campaign's own creature rather than hitting a missing key.
    entry.setdefault("unmodelled", [])
    entry.setdefault("provenance", entry["source"])
    return entry


@server.tool()
def lookup_rule(topic: str = "") -> dict[str, Any]:
    """Look up a loaded condition, spell, creature, item, or terrain kind.
    Omit ``topic`` to list all.

    Searches whatever content is loaded, bundled or not, and every entry names the
    pack it came from in ``source``. A miss means the subject is not loaded — check
    content_status before concluding it does not exist.
    """
    registry = _registry()
    if not topic:
        listing: dict[str, Any] = dict(registry.names())
        listing["builtin"] = registry.builtin.value
        listing["packs"] = [pack.label for pack in registry.packs]
        listing["provenance"] = sorted({pack.provenance for pack in registry.packs})
        return listing
    key = topic.strip().casefold()

    finders = (
        ("conditions", registry.condition_effects, _condition_entry),
        ("spells", registry.spells, _spell_entry),
        ("creatures", registry.creatures, _creature_entry),
        ("items", registry.items, _item_entry),
        ("terrain", registry.terrain_effects, _terrain_entry),
    )
    for _section, table, build in finders:
        for name in table:
            if name.casefold() == key:
                return build(registry, name)

    raise ToolError(
        f"nothing loaded for {topic!r}. Call lookup_rule with no topic to list what "
        f"is available, or content_status to see which packs are loaded."
    )


# --- stateful encounters ---------------------------------------------------
@server.tool()
def encounter_create(
    combatants: list[dict[str, Any]],
    seed: int | None = None,
    movement_rule: str = "5-5-5",
    map: dict[str, Any] | None = None,
    map_id: str | None = None,
) -> dict[str, Any]:
    """Start an encounter and roll initiative, optionally on a battle map.

    Each combatant is either ``{"monster": "Goblin Warrior", "label": "Goblin A",
    "team": "monsters", "position": [15, 0]}`` for a bundled stat block, or an
    explicit description with at least name, team, ac, and max_hp. Names must be
    unique — they identify combatants in every later call. A position is ``[x, y]``
    in feet on a flat plane; a bare number is accepted and means feet along the
    x-axis. ``movement_rule`` is how diagonals are measured: "5-5-5" (the default)
    or "5-10-5" (every second diagonal costs double).

    ``map`` puts the fight on a grid of 5-foot squares: ``{"width", "height"}``
    plus either ``"rows"`` (a list of strings, one per row, top row first) with a
    ``"legend"`` mapping each character to a terrain kind, or a ``"terrain"``
    list of ``{"kind", "squares": [[x, y], ...]}`` overrides on
    ``"default_terrain"``. ``"features"`` lists doors and the like:
    ``{"name", "square", "kind"?, "initially_open"?}``. With a map, terrain
    costs movement, walls block sight and routes, cover adjusts AC, and starting
    positions must be on-map, passable, and unoccupied; positions snap to their
    square. Without one, the plane is open and featureless.

    ``map_id`` fights on a loaded map session (see map_generate and map_load)
    instead of an inline spec — one or the other, never both. The fight captures
    the document by value: a later map_edit does not reach into it, and the
    ``map_source`` field here and in encounter_state reports the captured
    generation and whether the live map has since moved on.
    """
    used = _resolve_seed(seed)
    rng = Random(used)
    content = _content()
    battle_map, map_source = _resolve_battle_map(map, map_id)
    try:
        encounter = _new_encounter(
            _combatants(combatants, content.registry), rng, content.registry,
            movement_rule=_movement_rule(movement_rule),
            battle_map=battle_map,
        )
    except EncounterError as error:
        raise ToolError(str(error)) from error
    encounter_id = _new_encounter_id()
    session = _Session(
        encounter=encounter, rng=rng, seed=used,
        content_generation=content.generation,
    )
    if map_source is not None:
        session.map_id = str(map_source["map_id"])
        session.map_generation = int(map_source["map_generation"])
        session.map_sha256 = str(map_source["map_sha256"])
    _SESSIONS[encounter_id] = session
    result: dict[str, Any] = {
        "encounter_id": encounter_id,
        "seed": used,
        "content_generation": content.generation,
        "state": encounter.state(),
        "log": [event.as_dict() for event in encounter.log],
    }
    if map_source is not None:
        result["map_source"] = map_source
    if content.startup_error:
        result["content_warning"] = (
            "configured content failed to load; this fight uses the bundled slice "
            "only. See content_status."
        )
    return result


@server.tool()
def encounter_state(encounter_id: str) -> dict[str, Any]:
    """The authoritative state of an encounter. Narrate from this, not from memory.

    Each combatant's ``position`` is ``[x, y]`` in feet on the plane. For a
    fight created from a ``map_id``, ``map_source`` reports the map generation
    it captured and whether the live map has been edited since (``stale``).
    """
    session = _session(encounter_id)
    state = session.encounter.state()
    state["map_source"] = _map_source_of(session)
    return state


@server.tool()
def encounter_log(
    encounter_id: str,
    since: int = 0,
    limit: int = 500,
    include_actions: bool = True,
) -> dict[str, Any]:
    """The full event history of an encounter, paged, with the actions that made it.

    Events come back from ``since`` (a ``seq`` value) in pages of at most ``limit``;
    ``next`` is the ``since`` for the following page, or null on the last one.
    ``actions`` lists every successful act and advance in order — applied against
    the reported seed and the same combatants, they reproduce the log exactly.
    ``encounter_state`` stays the view of now; this is the record of how the fight
    got there.
    """
    session = _session(encounter_id)
    if since < 0:
        raise ToolError(f"since must not be negative: {since}")
    if limit < 1:
        raise ToolError(f"limit must be at least 1: {limit}")
    log = session.encounter.log
    page = log[since:since + limit]
    result: dict[str, Any] = {
        "encounter_id": encounter_id,
        "seed": session.seed,
        "format": "fivee-sim-log/1",
        "total_events": len(log),
        "since": since,
        "events": [event.as_dict() for event in page],
        "next": since + len(page) if since + len(page) < len(log) else None,
        "total_actions": len(session.encounter.actions),
    }
    if include_actions:
        result["actions"] = [record.as_dict() for record in session.encounter.actions]
    return result


@server.tool()
def encounter_act(
    encounter_id: str,
    kind: str,
    target: str | None = None,
    attack: str | None = None,
    item: str | None = None,
    spell: str | None = None,
    slot_level: int | None = None,
    to_position: int | list[int] | None = None,
    targets: list[str] | None = None,
    center: int | list[int] | None = None,
    direction: list[int] | None = None,
    toward: str | list[int] | None = None,
    path: list[list[int]] | None = None,
    feature: str | None = None,
) -> dict[str, Any]:
    """Take an action for the creature whose turn it is.

    ``kind`` is attack, cast, use_item, move, dash, disengage, dodge, or interact.
    Attacks need ``target``; casting needs ``spell`` plus an aim — ``target`` or
    ``targets`` for named creatures, ``center`` for a sphere (or a cube's minimum
    corner), ``direction`` for a cone (one of the eight unit offsets, such as
    ``[1, 0]`` or ``[-1, 1]``), ``toward`` for a line (a combatant name or a
    point). Using an item needs ``item``, and ``target`` unless the item is
    self-directed; moving needs ``to_position``; interacting — opening or closing
    a map feature, free once per turn — needs ``feature``. A position —
    ``to_position``, ``center``, or a ``toward`` point — is ``[x, y]`` in feet on
    the plane; a bare number is accepted and means feet along the x-axis. On a
    battle map a move routes itself around walls and enemies; ``path`` optionally
    pins the exact route as ``[x, y]`` waypoints, one per square. Illegal actions
    are refused with the reason rather than silently adjusted.
    """
    session = _session(encounter_id)
    try:
        action_kind = ActionKind(kind)
    except ValueError as error:
        allowed = ", ".join(item.value for item in ActionKind)
        raise ToolError(f"kind must be one of: {allowed}") from error
    waypoints: list[tuple[int, int]] = []
    for step in path or []:
        point = _point(step, "each path waypoint")
        if isinstance(point, int):
            raise ToolError("each path waypoint must be an [x, y] pair of feet")
        waypoints.append(point)
    aim_direction: tuple[int, int] | None = None
    if direction is not None:
        parsed = _point(direction, "direction")
        if isinstance(parsed, int):
            raise ToolError("direction must be an [x, y] unit offset such as [1, 0]")
        aim_direction = parsed
    aim_toward: str | tuple[int, int] | None = None
    if toward is not None:
        if isinstance(toward, str):
            aim_toward = toward
        else:
            parsed = _point(toward, "toward")
            if isinstance(parsed, int):
                raise ToolError("toward must be a combatant name or an [x, y] point")
            aim_toward = parsed
    action = Action(
        kind=action_kind,
        target=target,
        attack=attack,
        item=item,
        spell=spell,
        slot_level=slot_level,
        to_position=(
            _point(to_position, "to_position") if to_position is not None else None
        ),
        targets=tuple(targets or ()),
        center=_point(center, "center") if center is not None else None,
        direction=aim_direction,
        toward=aim_toward,
        path=tuple(waypoints),
        feature=feature,
    )
    try:
        events = session.encounter.act(action, session.rng)
    except EncounterError as error:
        raise ToolError(str(error)) from error
    return {
        "events": [event.as_dict() for event in events],
        "state": session.encounter.state(),
    }


@server.tool()
def encounter_advance(encounter_id: str) -> dict[str, Any]:
    """End the current turn and begin the next, rolling any death saves that are due."""
    session = _session(encounter_id)
    events = session.encounter.advance(session.rng)
    return {
        "events": [event.as_dict() for event in events],
        "state": session.encounter.state(),
    }


# --- maps ------------------------------------------------------------------
#: A map at or under this many squares renders inline in tool results; a larger
#: one returns ``render: null`` and a pointer at map_render's viewports.
_INLINE_RENDER_CELLS = 4000
_TOKEN_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _on_document(document: MapDocument, square: Square) -> bool:
    return 0 <= square[0] < document.grid.width and 0 <= square[1] < document.grid.height


def _encounter_tokens(
    document: MapDocument, encounter_id: str
) -> tuple[dict[Square, str], dict[str, str]]:
    """Combatant overlay marks for a render: letters by initiative, ``x`` downed.

    Only combatants standing inside the document's bounds appear. Downed
    bodies are placed first, so a conscious combatant sharing a square wins
    the cell, matching the rule that a downed body blocks nothing.
    """
    fight = _session(encounter_id).encounter
    tokens: dict[Square, str] = {}
    letters: dict[str, str] = {}
    for name in fight.order:
        creature = fight.creatures[name]
        if creature.conscious:
            continue
        square = to_square(as_point(creature.position))
        if _on_document(document, square):
            tokens[square] = "x"
    index = 0
    for name in fight.order:
        creature = fight.creatures[name]
        if not creature.conscious:
            continue
        square = to_square(as_point(creature.position))
        if not _on_document(document, square):
            continue
        if index < len(_TOKEN_LETTERS):
            letter = _TOKEN_LETTERS[index]
            letters[letter] = name
        else:  # more conscious combatants than letters; mark without naming
            letter = "?"
        index += 1
        tokens[square] = letter
    return tokens, letters


def _edit_render(before: MapDocument, after: MapDocument) -> dict[str, Any]:
    """A render sized to what an edit touched.

    The whole map when it is small enough to inline; otherwise the bounding
    box of every changed cell and feature, downsampled just far enough to fit
    the render budget.
    """
    width, height = after.grid.width, after.grid.height
    if width * height <= _INLINE_RENDER_CELLS:
        return _map_service.render_ascii(after)
    xs: list[int] = []
    ys: list[int] = []
    if (before.grid.width, before.grid.height) != (width, height):
        xs = [0, width - 1]
        ys = [0, height - 1]
    else:
        legends_match = dict(before.legend) == dict(after.legend)
        for yy, (old_row, new_row) in enumerate(zip(before.tiles, after.tiles, strict=True)):
            if legends_match and old_row == new_row:
                continue
            for xx, (old_char, new_char) in enumerate(zip(old_row, new_row, strict=True)):
                if before.legend[old_char] != after.legend[new_char]:
                    xs.append(xx)
                    ys.append(yy)
        olds = {feature.id: feature for feature in before.features}
        news = {feature.id: feature for feature in after.features}
        for feature_id in set(olds) | set(news):
            old, new = olds.get(feature_id), news.get(feature_id)
            if old == new:
                continue
            for feature in (old, new):
                if feature is not None:
                    xs.append(feature.at[0])
                    ys.append(feature.at[1])
        if not xs:
            xs = [0, width - 1]
            ys = [0, height - 1]
    x0, y0 = min(xs), min(ys)
    box_w, box_h = max(xs) - x0 + 1, max(ys) - y0 + 1
    downsample = 1
    while -(-box_w // downsample) * -(-box_h // downsample) > _map_service.RENDER_BUDGET:
        downsample += 1
    return _map_service.render_ascii(
        after, x=x0, y=y0, width=box_w, height=box_h, downsample=downsample
    )


@server.tool()
def map_generate(
    kind: str,
    params: dict[str, Any] | None = None,
    seed: int | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Generate a battle map — "dungeon", "caves", or "overland" — under a seed.

    The seed is always reported; the same seed, kind, and params reproduce the
    map exactly. ``params`` overrides the kind's defaults (call with an unknown
    key to be told the valid ones), and the result's ``params`` comes back
    fully resolved. The map is held in this session under ``map_id`` for
    map_render, map_edit, map_query, map_save, and encounter_create; small maps
    include an inline render, larger ones return ``render: null`` and a note —
    use map_render with a viewport or downsample.
    """
    used = _resolve_seed(seed)
    try:
        document = _map_service.generate(kind, params, used, name=name)
    except ValueError as error:
        raise ToolError(str(error)) from error
    map_id = _new_map_id()
    _MAPS[map_id] = _MapSession(document=document)
    result: dict[str, Any] = {
        "map_id": map_id,
        "seed": used,
        "kind": kind,
        "name": document.name,
        "params": dict(document.provenance.params),
        "summary": _map_summary(document),
        "provenance": as_payload(document)["provenance"],
    }
    if document.grid.width * document.grid.height <= _INLINE_RENDER_CELLS:
        result["render"] = _map_service.render_ascii(document)
    else:
        result["render"] = None
        result["note"] = (
            "the map is too large to render inline; call map_render with a viewport "
            "(x, y, width, height) or a downsample factor"
        )
    return result


@server.tool()
def map_load(
    path: str | None = None,
    document: dict[str, Any] | None = None,
    replace: str | None = None,
) -> dict[str, Any]:
    """Load a map document into the session — from a file, or given inline.

    Exactly one of ``path`` and ``document``. Validation is strict and a
    failure reports every diagnostic; warnings ride along with success.
    ``replace`` rebinds an existing map_id to the loaded document (bumping its
    generation) instead of minting a new id — the way to re-read a file after
    an external editor saved it. ``sha256`` is the canonical document hash.
    """
    if (path is None) == (document is None):
        raise ToolError("give exactly one of 'path' (a file) or 'document' (inline JSON)")
    terrain = _registry().terrain_effects
    try:
        if path is not None:
            loaded, warnings = _map_service.load_file(path, terrain=terrain)
        else:
            assert document is not None
            loaded, warnings = _map_service.parse_payload(
                document, source="inline", terrain=terrain
            )
    except ValueError as error:
        raise ToolError(str(error)) from error
    if replace is not None:
        session = _map_session(replace)
        session.document = loaded
        session.generation += 1
        session.path = path
        map_id = replace
    else:
        map_id = _new_map_id()
        _MAPS[map_id] = _MapSession(document=loaded, path=path)
    return {
        "map_id": map_id,
        "name": loaded.name,
        "summary": _map_summary(loaded),
        "warnings": [warning.as_dict() for warning in warnings],
        "provenance": as_payload(loaded)["provenance"],
        "sha256": sha256_of(_serialize_map(loaded)),
    }


@server.tool()
def map_save(map_id: str, path: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Write a loaded map to disk as canonical JSON, refusing silent overwrites.

    ``path`` defaults to ``<maps root>/<slug-of-name>.json`` under the
    project's ``.fivee-sim/maps`` (or ``FIVEE_SIM_MAPS``). An existing file is
    only replaced when ``overwrite`` is true. Returns the written path, byte
    count, and sha256 — the hash to quote when handing the file elsewhere.
    """
    session = _map_session(map_id)
    target = (
        path
        if path is not None
        else str(_map_service.maps_root() / f"{slugify(session.document.name)}.json")
    )
    try:
        saved = _map_service.save_file(session.document, target, overwrite=overwrite)
    except (OSError, ValueError) as error:
        raise ToolError(str(error)) from error
    session.path = str(saved["path"])
    return {**saved, "map_id": map_id, "provenance": as_payload(session.document)["provenance"]}


@server.tool()
def map_render(
    map_id: str,
    x: int = 0,
    y: int = 0,
    width: int | None = None,
    height: int | None = None,
    downsample: int = 1,
    show_features: bool = True,
    encounter_id: str | None = None,
) -> dict[str, Any]:
    """Render a viewport of a loaded map as rows of glyphs.

    The viewport (``x``, ``y``, ``width``, ``height``, in squares) is clamped
    to the map; ``downsample=k`` renders each k-by-k block as its majority
    terrain. A render over 10000 cells is refused — narrow the viewport or
    raise the downsample. Overlays: ``+`` closed door, ``/`` open door, ``<``
    and ``>`` stairs, ``@`` spawn. With ``encounter_id``, conscious combatants
    overlay as letters in initiative order (``tokens`` maps letter to name)
    and downed ones as ``x`` — positions come from that encounter's state, so
    render after acting, not before.
    """
    session = _map_session(map_id)
    tokens: dict[Square, str] = {}
    letters: dict[str, str] = {}
    if encounter_id is not None:
        tokens, letters = _encounter_tokens(session.document, encounter_id)
    try:
        rendered = _map_service.render_ascii(
            session.document,
            x=x, y=y, width=width, height=height,
            downsample=downsample, show_features=show_features,
            tokens=tokens or None,
        )
    except ValueError as error:
        raise ToolError(str(error)) from error
    result: dict[str, Any] = {"map_id": map_id, "generation": session.generation, **rendered}
    if encounter_id is not None:
        result["tokens"] = letters
    return result


@server.tool()
def map_edit(map_id: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Edit a loaded map atomically: all operations apply, or none do.

    Each operation is an object with an ``op`` key: ``set_terrain`` {rect:
    [x, y, w, h], terrain}, ``paint`` {cells: [[x, y], ...], terrain},
    ``line`` {from, to, terrain}, ``carve_corridor`` {from, to, terrain?,
    horizontal_first?}, ``add_feature`` {feature}, ``remove_feature`` {id},
    ``toggle_door`` {at}, ``resize`` {width, height, anchor?, fill?},
    ``set_legend`` {glyph, terrain}, ``set_name`` {name}. A bad operation is
    refused with its index and changes nothing. A successful edit bumps the
    map's generation, marks it edited, and returns a render covering what
    changed. Fights already created from this map keep the version they
    captured — their encounter_state reports ``stale`` instead.
    """
    session = _map_session(map_id)
    before = session.document
    try:
        after = _map_service.apply_edits(
            before, operations, terrain=_registry().terrain_effects
        )
    except ValueError as error:
        raise ToolError(str(error)) from error
    if after is not before:
        session.document = after
        session.generation += 1
    return {
        "applied": len(operations),
        "map_id": map_id,
        "generation": session.generation,
        "edited": after.provenance.edited,
        "summary": _map_summary(after),
        "render": _edit_render(before, after),
    }


@server.tool()
def map_query(
    map_id: str,
    query: str,
    frm: list[int] | None = None,
    to: list[int] | None = None,
) -> dict[str, Any]:
    """Geometry over a loaded map: "distance", "line_of_sight", or "path".

    ``frm`` and ``to`` are ``[x, y]`` square indices (``frm`` because ``from``
    is a reserved word in the implementation language). Doors count in their
    recorded default state and nothing is occupied — for questions inside a
    fight, use the encounter tools, which see live doors and creatures.
    ``distance`` answers in feet; ``line_of_sight`` is a boolean; ``path``
    returns the squares and cost in feet, or ``reachable: false``.
    """
    session = _map_session(map_id)

    def _square_arg(value: list[int] | None, what: str) -> Square:
        if not (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(part, int) and not isinstance(part, bool) for part in value)
        ):
            raise ToolError(f"{what} must be an [x, y] pair of squares")
        return (value[0], value[1])

    origin = _square_arg(frm, "frm")
    target = _square_arg(to, "to")
    try:
        answer = _map_service.query(
            session.document, query, origin, target,
            terrain=_registry().terrain_effects,
        )
    except (UnknownTerrain, ValueError) as error:
        raise ToolError(str(error)) from error
    return {"map_id": map_id, **answer}


# --- content ---------------------------------------------------------------
def _status() -> dict[str, Any]:
    content = _content()
    stale = [
        {"encounter_id": name, "content_generation": session.content_generation}
        for name, session in sorted(_SESSIONS.items())
        if session.content_generation != content.generation
    ]
    status: dict[str, Any] = {
        "generation": content.generation,
        "configured_paths": list(content.configured),
        "environment": {
            CONTENT_ENV: environment_paths() or None,
            BUILTIN_ENV: content.registry.builtin.value,
        },
        **content.registry.summary(),
    }
    if stale:
        # A fight keeps the content it started with, so this is not a fault — it is
        # the divergence made visible, which is the only way narration stays honest.
        status["encounters_on_older_content"] = stale
    if content.startup_error:
        status["startup_error"] = content.startup_error
    return status


@server.tool()
def content_status() -> dict[str, Any]:
    """What content is loaded, from where, and under which mode.

    Use this before telling anyone what the engine supports: with packs loaded, or
    with the bundled slice excluded, the answer is whatever this reports and not what
    ships by default. It also names any encounter still running on older content.
    """
    return _status()


@server.tool()
def content_validate(
    paths: list[str] | None = None,
    builtin: str | None = None,
) -> dict[str, Any]:
    """Report problems with content packs without loading them. The authoring aid.

    Give ``paths`` to check specific files or directories, or omit it to re-check what
    is currently configured. Every diagnostic names the pack, section, record, and
    field, and separates errors from warnings.
    """
    content = _content()
    candidates = list(paths) if paths is not None else list(content.configured)
    diagnostics = _validate_content(
        candidates, builtin=_mode(builtin, default=content.registry.builtin)
    )
    errors = [d for d in diagnostics if d.severity.value == "error"]
    warnings = [d for d in diagnostics if d.severity.value == "warning"]
    return {
        "checked": candidates,
        "builtin": _mode(builtin, default=content.registry.builtin).value,
        "ok": not errors,
        "errors": [d.as_dict() for d in errors],
        "warnings": [d.as_dict() for d in warnings],
        "summary": (
            "no problems found" if not diagnostics
            else f"{len(errors)} error(s), {len(warnings)} warning(s)"
        ),
    }


@server.tool()
def content_configure(
    paths: list[str] | None = None,
    builtin: str | None = None,
    add: bool = False,
) -> dict[str, Any]:
    """Load content packs and/or switch whether the bundled SRD slice is included.

    ``paths`` names files or directories of ``*.json`` packs and replaces the current
    set unless ``add`` is true. ``builtin`` is "include" or "exclude"; omit either
    argument to leave it as it is.

    Nothing changes unless the new content loads cleanly — a failed call reports every
    diagnostic and leaves the working content in place. Encounters already in progress
    keep resolving under the content they started with; only new ones use the result.
    """
    content = _content()
    if paths is None and builtin is None:
        raise ToolError("give 'paths', 'builtin', or both — there is nothing to change")
    mode = _mode(builtin, default=content.registry.builtin)
    if paths is None:
        configured = list(content.configured)
    elif add:
        configured = [*content.configured, *paths]
    else:
        configured = list(paths)

    try:
        registry = load_packs(configured, builtin=mode)
    except ContentError as error:
        raise ToolError(
            f"content not changed. {error}"
        ) from error

    global _CONTENT
    _CONTENT = _Content(
        registry=registry,
        generation=content.generation + 1,
        configured=tuple(configured),
        startup_error="",
    )
    return {"changed": True, **_status()}


# --- analytics -------------------------------------------------------------
@server.tool()
def simulate_rounds(
    combatants: list[dict[str, Any]],
    iterations: int = 500,
    seed: int = 0,
    max_rounds: int = 20,
    movement_rule: str = "5-5-5",
    map: dict[str, Any] | None = None,
    map_id: str | None = None,
) -> dict[str, Any]:
    """Auto-play the same encounter many times and report win rates and length.

    Combatant specs match ``encounter_create``, as do ``movement_rule``, the
    inline ``map`` spec, and ``map_id`` (a loaded map session; one or the
    other, not both) — with a map, every iteration fights on it: terrain
    costs, cover, sight, and pathfinding all apply, and doors reset to their
    initial state between iterations. Iteration ``i`` uses ``seed + i``, so one
    iteration reproduces a single hand-played encounter at that seed. With
    ``map_id`` the result's ``map_source`` records the exact map generation
    and hash the batch ran on.
    """
    specs = list(combatants)
    # The registry is captured once, here. Resolving content per iteration would let
    # a reconfiguration land mid-batch and make the result unreproducible from its
    # seed, which is the one property these numbers rest on.
    registry = _registry()
    battle_map, map_source = _resolve_battle_map(map, map_id)

    def factory() -> list[Creature]:
        return _combatants(specs, registry)

    try:
        result = _simulate_rounds(
            factory,
            iterations=iterations,
            seed=seed,
            max_rounds=max_rounds,
            spellbook=dict(registry.spells),
            items=dict(registry.items),
            condition_effects=registry.condition_effects,
            movement_rule=_movement_rule(movement_rule),
            battle_map=battle_map,
            terrain_effects=registry.terrain_effects,
        )
    except (ValueError, EncounterError) as error:
        raise ToolError(str(error)) from error
    if map_source is not None:
        result["map_source"] = map_source
    return result


@server.tool()
def simulate_dpr(
    build: dict[str, Any],
    target_ac: int,
    rounds: int = 3,
    iterations: int = 1000,
    seed: int = 0,
    distance: int = 5,
) -> dict[str, Any]:
    """Measure the damage a build lands over several rounds against a given AC.

    The target is a passive dummy with enough hit points to absorb the whole run,
    driven through the real encounter stepper — so advantage, criticals, and
    resistances apply exactly as they would in play.

    ``distance`` is how far off the dummy stands, defaulting to melee reach. It must
    be greater than zero for an area caster: the policy refuses to catch the caster
    in its own blast, and a dummy standing on the attacker leaves no placement that
    catches one without the other. The ``actions`` field reports what the build
    actually did, so a spell the policy declined to cast is visible rather than
    silently absent from the damage figure.
    """
    spec = dict(build)
    registry = _registry()

    def attacker() -> Creature:
        creature = _creature_from_spec(spec, registry)
        creature.team = "attacker"
        return creature

    try:
        return _simulate_dpr(
            attacker,
            target_ac=target_ac,
            rounds=rounds,
            iterations=iterations,
            seed=seed,
            distance=distance,
            spellbook=dict(registry.spells),
            items=dict(registry.items),
            condition_effects=registry.condition_effects,
        )
    except (ValueError, EncounterError) as error:
        raise ToolError(str(error)) from error


def main() -> None:
    """Entry point for the stdio server."""
    server.run("stdio")


if __name__ == "__main__":
    main()
