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

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from random import Random
from typing import Any

from mcp.server.mcpserver import MCPServer

from .. import __version__
from ..analytics.montecarlo import simulate_dpr as _simulate_dpr
from ..analytics.montecarlo import simulate_rounds as _simulate_rounds
from ..analytics.scenario import response_window as _response_window
from ..analytics.scenario import travel_timing as _travel_timing
from ..content import (
    BUILTIN_ENV,
    CONTENT_ENV,
    BuiltinMode,
    ContentError,
    ContentRegistry,
    DataError,
    builtin_mode,
    builtin_registry,
    environment_paths,
    load_packs,
    make_creature,
    registry_from_snapshot,
)
from ..content import validate as _validate_content
from ..editor.cli import read_state, state_file_for
from ..editor.http_server import TOKEN_HEADER
from ..kernel.actions import AttackKind, RiderExpiry
from ..kernel.dice import Advantage, Dice, roll_d20, roll_dice
from ..kernel.grid import (
    DiagonalRule,
    MovementMode,
    Point,
    Square,
    TerrainEffect,
    UnknownTerrain,
    as_point,
    to_square,
)
from ..kernel.rules import Ability, DamageType, Size, make_d20_test
from ..map_document import GROUND_LEVEL, MapDocument, MapLevel, as_payload, to_grid
from ..map_document import serialize as _serialize_map
from ..model.battlemap import BattleMap, MapFeature
from ..model.creature import AttackOption, Creature, DeathRule
from ..model.encounter import Action, ActionKind, Encounter, EncounterError
from ..service import encounter_journal as _journal_service
from ..service import maps as _map_service
from ..service import replay as _replay_service
from ..service import uvtt as _uvtt_service
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
    #: What a replay bundle needs, snapshotted the moment the encounter was
    #: built: the combatants as they stood before any turn, which features
    #: began open, and — for a session-map fight — the map document payload
    #: **by value**, so a later map_edit can never change an exported replay.
    #: Inline maps are kept separately so legacy v1 exports retain their
    #: documented neutral-plane behaviour while v2 exports are self-contained.
    initial_creatures: list[dict[str, Any]] = field(default_factory=list)
    initial_state: dict[str, Any] = field(default_factory=dict)
    initial_open_features: list[str] = field(default_factory=list)
    map_payload: dict[str, Any] | None = None
    inline_map_payload: dict[str, Any] | None = None
    normalized_combatants: list[dict[str, Any]] = field(default_factory=list)
    content_snapshot: dict[str, Any] = field(default_factory=dict)
    event_timestamps: list[str] = field(default_factory=list)
    state_history: list[dict[str, Any]] = field(default_factory=list)
    checkpoint_event_counts: list[int] = field(default_factory=list)
    checkpoint_timestamps: list[str] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    request_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    finalized: bool = False
    finalization_result: dict[str, Any] | None = None


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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _content_snapshot(registry: ContentRegistry) -> dict[str, Any]:
    """The exact content records and provenance an encounter captured."""
    records: dict[str, dict[str, Any]] = {}
    for section in ("spells", "conditions", "terrain", "items"):
        records[section] = {
            name: {
                "record": deepcopy(record),
                "source": registry.source_of(section, name),
            }
            for name, record in sorted(registry.records_for(section).items())
        }
    return {
        "builtin": registry.builtin.value,
        "packs": [pack.as_dict() for pack in registry.packs],
        "retained_conditions": list(registry.retained_conditions),
        "records": records,
    }


def _capture_checkpoint(session: _Session, timestamp: str) -> None:
    session.state_history.append(deepcopy(session.encounter.state()))
    session.checkpoint_event_counts.append(len(session.encounter.log))
    session.checkpoint_timestamps.append(timestamp)


def _journal_append(encounter_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _journal_service.append(encounter_id, payload)
    except _journal_service.JournalError as error:
        raise ToolError(str(error)) from error


def _cached_request(session: _Session, request_id: str | None) -> dict[str, Any] | None:
    if request_id is None:
        return None
    cached = session.request_results.get(request_id)
    if cached is None:
        return None
    if cached["status"] == "refused":
        raise ToolError(str(cached["error"]))
    result = cached.get("result")
    if not isinstance(result, Mapping):
        raise ToolError(f"request {request_id!r} has no recorded result")
    return deepcopy(dict(result))


def _attempt_started(
    encounter_id: str,
    session: _Session,
    operation: str,
    arguments: Mapping[str, Any],
    request_id: str | None,
) -> tuple[int, str]:
    timestamp = _utc_now()
    index = len(session.attempts)
    _journal_append(
        encounter_id,
        {
            "kind": "attempt",
            "timestamp": timestamp,
            "index": index,
            "operation": operation,
            "request_id": request_id,
            "arguments": deepcopy(dict(arguments)),
        },
    )
    return index, timestamp


def _attempt_finished(
    encounter_id: str,
    session: _Session,
    *,
    index: int,
    started_at: str,
    operation: str,
    arguments: Mapping[str, Any],
    request_id: str | None,
    status: str,
    result: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> None:
    timestamp = _utc_now()
    audit: dict[str, Any] = {
        "index": index,
        "timestamp": timestamp,
        "started_at": started_at,
        "operation": operation,
        "request_id": request_id,
        "arguments": deepcopy(dict(arguments)),
        "status": status,
    }
    if result is not None:
        audit["result"] = deepcopy(dict(result))
    if error is not None:
        audit["error"] = error
    try:
        _journal_append(encounter_id, {"kind": "result", **audit})
    except ToolError:
        # The caller cannot safely continue from state the durable record did
        # not acknowledge. Dropping the cache forces recovery from the valid
        # prefix on the next access.
        _SESSIONS.pop(encounter_id, None)
        raise
    session.attempts.append(audit)
    if request_id is not None:
        session.request_results[request_id] = audit


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
    while True:
        _NEXT_ID += 1
        candidate = f"enc-{_NEXT_ID}"
        try:
            exists = _journal_service.journal_path(candidate).exists()
        except _journal_service.JournalError:
            exists = False
        if candidate not in _SESSIONS and not exists:
            return candidate


def _resolve_seed(seed: int | None) -> int:
    """Expose service-level seed portability failures as MCP tool errors."""
    try:
        return resolve_seed(seed)
    except ValueError as error:
        raise ToolError(str(error)) from error


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
        session, _ = _recover_session(encounter_id)
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


def _storey_summary(level: MapLevel, legend: Mapping[str, str]) -> dict[str, Any]:
    """One storey's own counts, in the shape the document-wide totals take."""
    counts: dict[str, int] = {}
    for row in level.tiles:
        for char in row:
            kind = legend[char]
            counts[kind] = counts.get(kind, 0) + 1
    heights = [level.elevation.default, *level.elevation.squares.values()]
    return {
        "index": level.index,
        "name": level.name,
        "features": len(level.features),
        "terrain_counts": {kind: counts[kind] for kind in sorted(counts)},
        "elevation": {
            "default": level.elevation.default,
            "min": min(heights),
            "max": max(heights),
            "raised_squares": len(level.elevation.squares),
        },
    }


def _map_summary(document: MapDocument) -> dict[str, Any]:
    """What the document holds — every storey of it, and each one on its own.

    ``terrain_counts``, ``features`` and ``elevation`` span the whole map, so
    they answer the question ``levels`` has already told the reader to ask.
    They used to read the ground aliases, which made this the ground's summary
    under the map's name: an edit carrying ``level: 1`` reported level 0.

    ``elevation.default`` is a *plane's* datum and storeys rarely share one — a
    gallery ten feet up is exactly how a level sits above the one below. It is
    reported only when every storey agrees, and is ``None`` otherwise; each
    storey's own is in ``by_level``, which is where a caller reads one floor.
    """
    levels = [document.levels[index] for index in sorted(document.levels)]
    storeys = [_storey_summary(level, document.legend) for level in levels]
    counts: dict[str, int] = {}
    for storey in storeys:
        for kind, count in storey["terrain_counts"].items():
            counts[kind] = counts.get(kind, 0) + count
    defaults = {level.elevation.default for level in levels}
    return {
        "width": document.grid.width,
        "height": document.grid.height,
        "levels": sorted(document.levels),
        "features": sum(len(level.features) for level in levels),
        "terrain_counts": {kind: counts[kind] for kind in sorted(counts)},
        "elevation": {
            "default": next(iter(defaults)) if len(defaults) == 1 else None,
            "min": min(storey["elevation"]["min"] for storey in storeys),
            "max": max(storey["elevation"]["max"] for storey in storeys),
            "raised_squares": sum(len(level.elevation.squares) for level in levels),
        },
        "by_level": storeys,
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
    The shape matches :func:`_map_source_of`, so a caller reads ``stale`` off
    either tool's result — at capture time it is ``False`` by construction —
    plus ``sha256``, which only the capture can name.
    """
    if map_spec is not None and map_id is not None:
        raise ToolError("give 'map' (an inline spec) or 'map_id' (a loaded map), not both")
    if map_spec is not None:
        return _battle_map_from_spec(map_spec), None
    if map_id is not None:
        session = _map_session(map_id)
        return to_grid(session.document), {
            "map_id": map_id,
            "generation": session.generation,
            "current_generation": session.generation,
            "stale": False,
            "sha256": sha256_of(_serialize_map(session.document)),
        }
    return None, None


def _attack_from_spec(spec: dict[str, Any]) -> AttackOption:
    bonus_type = spec.get("bonus_damage_type")
    save_ability = spec.get("on_hit_save_ability")
    max_size = spec.get("on_hit_max_size")
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
            advantage_bonus_with_adjacent_ally=bool(
                spec.get("advantage_bonus_with_adjacent_ally", False)
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
            on_hit_max_size=Size(max_size) if max_size is not None else None,
            on_hit_attach=bool(spec.get("on_hit_attach", False)),
            attached_damage=(
                Dice.parse(str(spec["attached_damage"]))
                if spec.get("attached_damage") is not None else None
            ),
            attached_damage_type=(
                DamageType(spec["attached_damage_type"])
                if spec.get("attached_damage_type") is not None else None
            ),
            detach_after_damage=int(spec.get("detach_after_damage", 0)),
            provenance=str(spec.get("provenance", "caller-supplied")),
        )
    except KeyError as error:
        raise ToolError(f"attack spec is missing {error.args[0]!r}") from error
    except ValueError as error:
        raise ToolError(f"attack spec is invalid: {error}") from error


#: The two combatant spec shapes, kept apart because the lookup branch returns
#: before the constructor is reached and so reads none of the description keys —
#: folding them into one set would accept ``{"monster": "...", "ac": 22}`` and
#: silently ignore the AC, which is the very failure this guard exists to stop.
_LOOKUP_SPEC_KEYS = frozenset({"creature", "monster", "label", "team", "position", "level"})
_DESCRIBED_SPEC_KEYS = frozenset({
    "name", "team", "ac", "max_hp", "hp", "speed", "climb_speed", "swim_speed",
    "fly_speed", "terrain_cost_overrides", "darkvision", "blindsight", "death_rule",
    "size", "abilities", "save_bonuses", "attacks", "attacks_per_action",
    "bonus_actions", "surrender_when_last", "redirect_attack", "pack_tactics",
    "undead_fortitude", "spells",
    "spell_slots", "spell_save_dc", "spell_attack_bonus", "resistances", "immunities",
    "vulnerabilities", "items", "conditions", "position", "level", "provenance",
})


def _reject_unknown_keys(spec: dict[str, Any], allowed: frozenset[str]) -> None:
    """Refuse a combatant key nothing reads, the way every other spec already does.

    ``content.py`` refuses an unknown pack key and ``_map_from_spec`` an unknown
    map key, both for one reason: a key read with ``.get`` and a default cannot
    tell "omitted" from "misspelled", so the caller gets a creature that is not the
    one they described and nothing says so. An inline ``fly_speed`` produced a
    stirge that walked 10 feet with no flight and no warning; ``speeed`` would have
    produced the default 30 just as quietly.
    """
    for key in sorted(set(spec) - allowed):
        raise ToolError(
            f"unknown combatant key {key!r}. Valid keys: {', '.join(sorted(allowed))}"
        )


def _creature_from_spec(spec: dict[str, Any], registry: ContentRegistry) -> Creature:
    """Build a combatant from a loaded stat block or an explicit description.

    ``monster`` and ``creature`` are accepted interchangeably; the stat block is
    looked up in ``registry``, so which names resolve depends on what is loaded.
    """
    named = spec.get("creature", spec.get("monster"))
    if named is not None:
        _reject_unknown_keys(spec, _LOOKUP_SPEC_KEYS)
    else:
        _reject_unknown_keys(spec, _DESCRIBED_SPEC_KEYS)
    if named is not None:
        try:
            return make_creature(
                str(named),
                registry=registry,
                label=spec.get("label"),
                team=spec.get("team"),
                position=_point(spec.get("position", 0), "position"),
                level=int(spec.get("level", 0)),
            )
        except DataError as error:
            raise ToolError(str(error)) from error
    bonus_actions = frozenset(str(value) for value in spec.get("bonus_actions", []))
    unsupported_bonus_actions = sorted(bonus_actions - {"dash", "disengage"})
    if unsupported_bonus_actions:
        raise ToolError(
            "bonus_actions must contain only dash or disengage; got: "
            + ", ".join(unsupported_bonus_actions)
        )
    try:
        return Creature(
            name=str(spec["name"]),
            team=str(spec["team"]),
            ac=int(spec["ac"]),
            max_hp=int(spec["max_hp"]),
            hp=int(spec.get("hp", -1)),
            speed=int(spec.get("speed", 30)),
            climb_speed=int(spec.get("climb_speed", 0)),
            swim_speed=int(spec.get("swim_speed", 0)),
            fly_speed=int(spec.get("fly_speed", 0)),
            terrain_cost_overrides=frozenset(
                str(value) for value in spec.get("terrain_cost_overrides", [])
            ),
            darkvision=int(spec.get("darkvision", 0)),
            blindsight=int(spec.get("blindsight", 0)),
            # Read here rather than only accepted above: a key on the allow-list
            # that no constructor consumes is the same silent drop by another
            # route. Size gates attack riders like the Wolf's Prone.
            size=Size(spec["size"]) if "size" in spec else Size.MEDIUM,
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
            bonus_actions=bonus_actions,
            surrender_when_last=bool(spec.get("surrender_when_last", False)),
            redirect_attack=bool(spec.get("redirect_attack", False)),
            pack_tactics=bool(spec.get("pack_tactics", False)),
            undead_fortitude=bool(spec.get("undead_fortitude", False)),
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
            level=int(spec.get("level", 0)),
            death_rule=DeathRule(spec.get("death_rule", DeathRule.DEATH_SAVES)),
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
    "default_elevation", "elevation", "features",
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

    default_elevation = spec.get("default_elevation", 0)
    if isinstance(default_elevation, bool) or not isinstance(default_elevation, int):
        raise ToolError(
            f"'default_elevation' must be a whole number of feet, got "
            f"{default_elevation!r}"
        )
    elevation: dict[tuple[int, int], int] = {}
    raw_elevation = spec.get("elevation", [])
    if not isinstance(raw_elevation, list):
        raise ToolError(
            "'elevation' must be a list of [x, y, feet] entries, such as [[3, 4, 20]]"
        )
    for index, entry in enumerate(raw_elevation):
        if (
            not isinstance(entry, list)
            or len(entry) != 3
            or any(isinstance(value, bool) or not isinstance(value, int) for value in entry)
        ):
            raise ToolError(
                f"elevation entry #{index} must be [x, y, feet], got {entry!r}"
            )
        square = _square(entry[:2], f"elevation entry #{index} square", width, height)
        if square in elevation:
            raise ToolError(
                f"elevation entry #{index} names square [{square[0]}, {square[1]}] "
                f"again; it is already {elevation[square]} ft"
            )
        elevation[square] = int(entry[2])

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

    return BattleMap.flat(
        name=str(spec.get("name", "battle map")),
        width=width,
        height=height,
        default_terrain=default_terrain,
        terrain=terrain,
        default_elevation=default_elevation,
        elevation=elevation,
        features=features,
    )


def _initial_creatures(encounter: Encounter) -> list[dict[str, Any]]:
    """The combatants as the fight begins, in initiative order — the replay
    viewer's starting tokens. Captured right after construction, before any
    turn has moved or hurt anybody."""
    return [
        {
            "name": creature.name,
            "team": creature.team,
            "position": list(as_point(creature.position)),
            "hp": creature.hp,
            "max_hp": creature.max_hp,
        }
        for creature in (encounter.creatures[name] for name in encounter.order)
    ]


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
def _audited_primitive(
    *,
    encounter_id: str | None,
    request_id: str | None,
    operation: str,
    arguments: Mapping[str, Any],
    execute: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    if encounter_id is None:
        if request_id is not None:
            raise ToolError("request_id requires encounter_id")
        return execute()
    session = _session(encounter_id)
    cached = _cached_request(session, request_id)
    if cached is not None:
        return cached
    index, started_at = _attempt_started(
        encounter_id, session, operation, arguments, request_id
    )
    try:
        if session.finalized:
            raise ToolError(f"encounter {encounter_id!r} is finalized")
        result = execute()
        result["encounter_id"] = encounter_id
    except (ToolError, ValueError) as error:
        _attempt_finished(
            encounter_id,
            session,
            index=index,
            started_at=started_at,
            operation=operation,
            arguments=arguments,
            request_id=request_id,
            status="refused",
            error=str(error),
        )
        raise ToolError(str(error)) from error
    _attempt_finished(
        encounter_id,
        session,
        index=index,
        started_at=started_at,
        operation=operation,
        arguments=arguments,
        request_id=request_id,
        status="success",
        result=result,
    )
    return result


@server.tool()
def scenario_timing(
    distance_feet: int,
    speed_feet: int,
    dash: bool = False,
    start_delay_rounds: int = 0,
    response_after_rounds: int | None = None,
) -> dict[str, Any]:
    """Measure route arrival and, optionally, its lead over a timed response.

    This is scenario evidence rather than combat state: supply the authored route
    distance, movement speed, and response delay.  ``dash`` means the traveller
    spends its action to move twice its speed every round.
    """
    try:
        if response_after_rounds is None:
            return {
                "traveller": _travel_timing(
                    distance_feet=distance_feet,
                    speed_feet=speed_feet,
                    dash=dash,
                    start_delay_rounds=start_delay_rounds,
                ).as_dict()
            }
        return _response_window(
            distance_feet=distance_feet,
            speed_feet=speed_feet,
            dash=dash,
            start_delay_rounds=start_delay_rounds,
            response_after_rounds=response_after_rounds,
        )
    except ValueError as error:
        raise ToolError(str(error)) from error


@server.tool()
def roll(
    expression: str,
    advantage: str = "none",
    seed: int | None = None,
    encounter_id: str | None = None,
    request_id: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Roll a dice expression such as "2d6+3" or "d20", optionally with advantage.

    Advantage and disadvantage apply only to a single d20; they are ignored for
    other expressions because the rules attach them to d20 tests.
    """
    def execute() -> dict[str, Any]:
        used = _resolve_seed(seed)
        rng = Random(used)
        dice = Dice.parse(expression)
        state = _advantage(advantage)
        if dice.count == 1 and dice.faces == 20 and state is not Advantage.NONE:
            d20 = roll_d20(rng, state)
            result: dict[str, Any] = {
                "expression": str(dice),
                "seed": used,
                "advantage": state.value,
                "natural": d20.natural,
                "rolls": list(d20.rolls),
                "total": d20.natural + dice.modifier,
                "detail": d20.describe(),
            }
        else:
            rolled = roll_dice(dice, rng)
            result = {
                "expression": str(dice),
                "seed": used,
                "advantage": Advantage.NONE.value,
                "rolls": list(rolled.rolls),
                "total": rolled.total,
                "detail": rolled.describe(),
            }
        if label is not None:
            result["label"] = label
        return result

    return _audited_primitive(
        encounter_id=encounter_id,
        request_id=request_id,
        operation="roll",
        arguments={"expression": expression, "advantage": advantage, "seed": seed, "label": label},
        execute=execute,
    )


@server.tool()
def check(
    modifier: int,
    dc: int,
    advantage: str = "none",
    seed: int | None = None,
    encounter_id: str | None = None,
    request_id: str | None = None,
    ability: str | None = None,
    skill: str | None = None,
) -> dict[str, Any]:
    """Make an ability or skill check, optionally attached to an encounter."""
    def execute() -> dict[str, Any]:
        if ability is not None:
            Ability(ability)
        if skill is not None and not skill.strip():
            raise ToolError("skill must not be blank")
        used = _resolve_seed(seed)
        test = make_d20_test(
            Random(used), modifier=modifier, dc=dc, advantage=_advantage(advantage)
        )
        result: dict[str, Any] = {
            "seed": used,
            "natural": test.roll.natural,
            "total": test.total,
            "dc": dc,
            "success": test.success,
            "detail": test.describe(),
        }
        if ability is not None:
            result["ability"] = ability
        if skill is not None:
            result["skill"] = skill
        return result

    return _audited_primitive(
        encounter_id=encounter_id,
        request_id=request_id,
        operation="check",
        arguments={
            "modifier": modifier, "dc": dc, "advantage": advantage, "seed": seed,
            "ability": ability, "skill": skill,
        },
        execute=execute,
    )


@server.tool()
def save(
    modifier: int,
    dc: int,
    advantage: str = "none",
    auto_fail: bool = False,
    seed: int | None = None,
    encounter_id: str | None = None,
    request_id: str | None = None,
    ability: str | None = None,
) -> dict[str, Any]:
    """Make a saving throw. ``auto_fail`` covers conditions that forfeit the save."""
    def execute() -> dict[str, Any]:
        if ability is not None:
            Ability(ability)
        used = _resolve_seed(seed)
        test = make_d20_test(
            Random(used),
            modifier=modifier,
            dc=dc,
            advantage=_advantage(advantage),
            auto_fail=auto_fail,
        )
        result: dict[str, Any] = {
            "seed": used,
            "natural": test.roll.natural,
            "total": test.total,
            "dc": dc,
            "success": test.success,
            "auto_failed": test.auto_failed,
            "detail": test.describe(),
        }
        if ability is not None:
            result["ability"] = ability
        return result

    return _audited_primitive(
        encounter_id=encounter_id,
        request_id=request_id,
        operation="save",
        arguments={
            "modifier": modifier, "dc": dc, "advantage": advantage,
            "auto_fail": auto_fail, "seed": seed, "ability": ability,
        },
        execute=execute,
    )


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
    # ``unmodelled`` is present even when empty. The skill tells the assistant to check it
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
def _action_from_journal(arguments: Mapping[str, Any]) -> Action:
    def point(name: str) -> int | Point | None:
        value = arguments.get(name)
        if value is None or isinstance(value, int):
            return value
        return (int(value[0]), int(value[1]))

    toward = arguments.get("toward")
    aimed: str | Point | None
    if isinstance(toward, str) or toward is None:
        aimed = toward
    else:
        aimed = (int(toward[0]), int(toward[1]))
    direction = arguments.get("direction")
    return Action(
        kind=ActionKind(str(arguments["kind"])),
        target=arguments.get("target"),
        attack=arguments.get("attack"),
        item=arguments.get("item"),
        spell=arguments.get("spell"),
        slot_level=arguments.get("slot_level"),
        to_position=point("to_position"),
        targets=tuple(arguments.get("targets") or ()),
        center=point("center"),
        direction=(
            (int(direction[0]), int(direction[1])) if direction is not None else None
        ),
        toward=aimed,
        path=tuple((int(step[0]), int(step[1])) for step in arguments.get("path") or ()),
        feature=arguments.get("feature"),
        set_open=arguments.get("set_open"),
        to_level=arguments.get("to_level"),
    )


def _recover_session(encounter_id: str) -> tuple[_Session, dict[str, str] | None]:
    global _NEXT_ID
    try:
        records, warning = _journal_service.read(encounter_id, repair_partial=True)
    except _journal_service.JournalError as error:
        known = ", ".join(sorted(_SESSIONS)) or "none"
        if "unknown encounter" in str(error):
            raise ToolError(
                f"unknown encounter {encounter_id!r}; active: {known}"
            ) from error
        raise ToolError(str(error)) from error
    if not records or records[0].get("kind") != "creation":
        raise ToolError(f"encounter journal {encounter_id!r} has no creation record")
    created = records[0]
    captured_content = created.get("content")
    if not isinstance(captured_content, Mapping):
        raise ToolError(f"encounter journal {encounter_id!r} has no content snapshot")
    try:
        registry = registry_from_snapshot(captured_content)
    except ContentError as error:
        raise ToolError(f"cannot recover {encounter_id!r}'s content: {error}") from error
    normalized = created.get("combatants")
    if not isinstance(normalized, list):
        raise ToolError(f"encounter journal {encounter_id!r} has no combatants")
    captured_map = created.get("map")
    battle_map: BattleMap | None = None
    if isinstance(captured_map, Mapping):
        try:
            document, _ = _map_service.parse_payload(
                captured_map,
                source=f"journal:{encounter_id}",
                terrain=registry.terrain_effects,
            )
        except (ValueError, DataError) as error:
            raise ToolError(f"cannot recover {encounter_id!r}'s map: {error}") from error
        battle_map = to_grid(document)
    seed = int(created["seed"])
    rng = Random(seed)
    encounter = _new_encounter(
        _combatants([dict(entry) for entry in normalized], registry),
        rng,
        registry,
        movement_rule=_movement_rule(str(created["movement_rule"])),
        battle_map=battle_map,
    )
    session = _Session(
        encounter=encounter,
        rng=rng,
        seed=seed,
        content_generation=int(created.get("content_generation", 0)),
        initial_creatures=_initial_creatures(encounter),
        initial_state=deepcopy(encounter.state()),
        initial_open_features=list(created.get("map_open_features", [])),
        normalized_combatants=deepcopy(normalized),
        content_snapshot=deepcopy(dict(captured_content)),
    )
    map_kind = created.get("map_kind")
    if map_kind == "loaded" and isinstance(captured_map, Mapping):
        session.map_payload = deepcopy(dict(captured_map))
        source = created.get("map_source")
        if isinstance(source, Mapping):
            session.map_id = str(source.get("map_id"))
            session.map_generation = int(source.get("generation", 0))
            session.map_sha256 = str(source.get("sha256", ""))
    elif map_kind == "inline" and isinstance(captured_map, Mapping):
        session.inline_map_payload = deepcopy(dict(captured_map))
    created_at = str(created["timestamp"])
    session.event_timestamps = [created_at] * len(encounter.log)
    _capture_checkpoint(session, created_at)

    pending: dict[int, dict[str, Any]] = {}
    for record in records[1:]:
        kind = record.get("kind")
        if kind == "attempt":
            pending[int(record["index"])] = record
            continue
        if kind == "finalized":
            session.finalized = True
            final = record.get("result")
            session.finalization_result = (
                deepcopy(dict(final)) if isinstance(final, Mapping) else {}
            )
            continue
        if kind != "result":
            continue
        index = int(record["index"])
        pending.pop(index, None)
        audit = {
            key: deepcopy(value)
            for key, value in record.items()
            if key not in {"kind", "previous_sha256", "sha256"}
        }
        operation = str(record.get("operation"))
        status = str(record.get("status"))
        if status == "success" and operation == "encounter_act":
            before = len(encounter.log)
            encounter.act(_action_from_journal(record["arguments"]), rng)
            timestamp = str(record["timestamp"])
            session.event_timestamps.extend([timestamp] * (len(encounter.log) - before))
            _capture_checkpoint(session, timestamp)
        elif status == "success" and operation == "encounter_advance":
            before = len(encounter.log)
            encounter.advance(rng)
            timestamp = str(record["timestamp"])
            session.event_timestamps.extend([timestamp] * (len(encounter.log) - before))
            _capture_checkpoint(session, timestamp)
        session.attempts.append(audit)
        request_id = record.get("request_id")
        if isinstance(request_id, str):
            session.request_results[request_id] = audit
    for index, record in sorted(pending.items()):
        session.attempts.append(
            {
                "index": index,
                "timestamp": record["timestamp"],
                "started_at": record["timestamp"],
                "operation": record["operation"],
                "request_id": record.get("request_id"),
                "arguments": deepcopy(record.get("arguments", {})),
                "status": "interrupted",
                "error": "the process stopped before recording a result",
            }
        )
    _SESSIONS[encounter_id] = session
    if encounter_id.startswith("enc-") and encounter_id[4:].isdigit():
        _NEXT_ID = max(_NEXT_ID, int(encounter_id[4:]))
    return session, warning


def _creation_response(encounter_id: str, session: _Session) -> dict[str, Any]:
    result: dict[str, Any] = {
        "encounter_id": encounter_id,
        "seed": session.seed,
        "content_generation": session.content_generation,
        "state": session.encounter.state(),
        "log": [event.as_dict() for event in session.encounter.log],
    }
    map_source = _map_source_of(session)
    if map_source is not None:
        if session.map_sha256:
            map_source["sha256"] = session.map_sha256
        result["map_source"] = map_source
    return result


def _creation_request(request_id: str) -> tuple[str, _Session] | None:
    for path in _journal_service.list_journals():
        encounter_id = path.stem
        try:
            records, _ = _journal_service.read(encounter_id)
        except _journal_service.JournalError:
            continue
        if records and records[0].get("request_id") == request_id:
            return encounter_id, _session(encounter_id)
    return None


@server.tool()
def encounter_create(
    combatants: list[dict[str, Any]],
    seed: int | None = None,
    movement_rule: str = "5-5-5",
    map: dict[str, Any] | None = None,
    map_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Start an encounter and roll initiative, optionally on a battle map.

    Each combatant is either ``{"monster": "Goblin Warrior", "label": "Goblin A",
    "team": "monsters", "position": [15, 0]}`` for a bundled stat block, or an
    explicit description with at least name, team, ac, and max_hp. A key the spec
    does not define is refused rather than ignored, so a misspelling or an
    unmodelled field such as ``fly_speed`` is reported instead of silently
    dropped; the two forms take different keys, and the refusal lists the ones
    that would have worked. Names must be
    unique — they identify combatants in every later call. A position is ``[x, y]``
    in feet on a flat plane; a bare number is accepted and means feet along the
    x-axis. ``movement_rule`` is how diagonals are measured: "5-5-5" (the default)
    or "5-10-5" (every second diagonal costs double).

    ``map`` puts the fight on a grid of 5-foot squares: ``{"width", "height"}``
    plus either ``"rows"`` (a list of strings, one per row, top row first) with a
    ``"legend"`` mapping each character to a terrain kind, or a ``"terrain"``
    list of ``{"kind", "squares": [[x, y], ...]}`` overrides on
    ``"default_terrain"``. ``"features"`` lists doors and the like:
    ``{"name", "square", "kind"?, "initially_open"?}``. Ground height is
    optional: ``"default_elevation"`` in feet plus an ``"elevation"`` list of
    ``[x, y, feet]`` for the squares that differ. With a map, terrain costs
    movement, walls block sight and routes, cover adjusts AC, and starting
    positions must be on-map, passable, and unoccupied; positions snap to their
    square. Without one, the plane is open and featureless.

    Height reaches movement and nothing else: a slope costs difficult terrain, a
    cliff costs a climb at an extra foot per foot, and climbing down costs what
    climbing up costs. Sight, cover, and area templates are measured flat, so
    high ground confers no advantage beyond the movement it costs to reach.

    ``map_id`` fights on a loaded map session (see map_generate and map_load)
    instead of an inline spec — one or the other, never both. The fight captures
    the document by value: a later map_edit does not reach into it, and the
    ``map_source`` field here and in encounter_state reports the captured
    generation and whether the live map has since moved on.
    """
    if request_id is not None:
        existing = _creation_request(request_id)
        if existing is not None:
            return _creation_response(*existing)
    used = _resolve_seed(seed)
    rng = Random(used)
    content = _content()
    battle_map, map_source = _resolve_battle_map(map, map_id)
    try:
        built_combatants = _combatants(combatants, content.registry)
        encounter = _new_encounter(
            built_combatants, rng, content.registry,
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
    session.initial_creatures = _initial_creatures(encounter)
    session.initial_state = deepcopy(encounter.state())
    session.normalized_combatants = [
        _replay_service.normalized_combatant_payload(creature)
        for creature in built_combatants
    ]
    session.content_snapshot = _content_snapshot(content.registry)
    created_at = _utc_now()
    session.event_timestamps = [created_at] * len(encounter.log)
    _capture_checkpoint(session, created_at)
    if encounter.map_state is not None:
        session.initial_open_features = sorted(encounter.map_state.open_features)
    if map_source is not None:
        session.map_id = str(map_source["map_id"])
        session.map_generation = int(map_source["generation"])
        session.map_sha256 = str(map_source["sha256"])
        # The payload, not the session reference: replay_export must see the
        # document as it stands now, whatever happens to the map later.
        session.map_payload = as_payload(_map_session(session.map_id).document)
    elif battle_map is not None:
        session.inline_map_payload = _replay_service.battle_map_payload(battle_map)
    _SESSIONS[encounter_id] = session
    captured_map = session.map_payload or session.inline_map_payload
    try:
        _journal_append(
            encounter_id,
            {
                "kind": "creation",
                "timestamp": created_at,
                "request_id": request_id,
                "encounter_id": encounter_id,
                "engine_version": __version__,
                "seed": used,
                "movement_rule": encounter.movement_rule.value,
                "content_generation": content.generation,
                "content": session.content_snapshot,
                "combatants": session.normalized_combatants,
                "map": captured_map,
                "map_kind": (
                    "loaded" if session.map_payload is not None
                    else "inline" if session.inline_map_payload is not None
                    else "none"
                ),
                "map_source": map_source,
                "map_open_features": session.initial_open_features,
                "initial_state": session.initial_state,
            },
        )
    except ToolError:
        _SESSIONS.pop(encounter_id, None)
        raise
    result = _creation_response(encounter_id, session)
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
def encounter_note(
    encounter_id: str,
    text: str,
    category: str = "note",
    request_id: str | None = None,
) -> dict[str, Any]:
    """Attach a durable narrative or adjudication note to an encounter."""
    def execute() -> dict[str, Any]:
        note = text.strip()
        label = category.strip()
        if not note:
            raise ToolError("note text must not be blank")
        if len(note) > 4000:
            raise ToolError("note text must be at most 4000 characters")
        if not label:
            raise ToolError("note category must not be blank")
        return {
            "encounter_id": encounter_id,
            "text": note,
            "category": label,
            "timestamp": _utc_now(),
        }

    return _audited_primitive(
        encounter_id=encounter_id,
        request_id=request_id,
        operation="encounter_note",
        arguments={"text": text, "category": category},
        execute=execute,
    )


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


def _execute_encounter_act(
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
    set_open: bool | None = None,
    to_level: int | None = None,
    movement_mode: str | None = None,
    as_bonus_action: bool = False,
) -> dict[str, Any]:
    """Take an action for the creature whose turn it is.

    ``kind`` is attack, cast, use_item, move, dash, disengage, dodge, stand, or
    interact. Attacks need ``target``; casting needs ``spell`` plus an aim —
    ``target`` or ``targets`` for named creatures, ``center`` for a sphere (or a
    cube's minimum corner), ``direction`` for a cone (one of the eight unit
    offsets, such as ``[1, 0]`` or ``[-1, 1]``), ``toward`` for a line (a
    combatant name or a point). Using an item needs ``item``, and ``target``
    unless the item is self-directed; moving needs ``to_position``; interacting —
    working a map fixture from adjacency — needs ``feature``, and optionally
    ``set_open`` to say which way rather than flipping whatever it finds, which
    is what to use when driving a fixture to a known state. A fixture may carry
    prerequisites that must stand open first, may cost the action rather than
    the free interaction, and may take an ability check; a fixture that reaches
    past its own square changes that ground the moment it moves, under whoever
    is standing on it;
    ``stand`` takes nothing and gets a Prone creature up, spending half its Speed
    from this turn's movement and no action. A position —
    ``to_position``, ``center``, or a ``toward`` point — is ``[x, y]`` in feet on
    the plane; a bare number is accepted and means feet along the x-axis. On a
    battle map a move routes itself around walls and enemies; ``path`` optionally
    pins the exact route as ``[x, y]`` waypoints, one per square. ``to_level``
    ends a move on another storey: walk to a stairway on your own level — the
    square named by ``to_position`` — and it carries you, charging the rise
    between the two floors as a climb. ``movement_mode`` selects walk, climb,
    swim, or fly; the creature must have that speed, and flight does not need a
    connector. Illegal actions are refused with the
    reason rather than silently adjusted.
    """
    session = _session(encounter_id)
    try:
        action_kind = ActionKind(kind)
    except ValueError as error:
        allowed = ", ".join(item.value for item in ActionKind)
        raise ToolError(f"kind must be one of: {allowed}") from error
    selected_mode: MovementMode | None = None
    if movement_mode is not None:
        try:
            selected_mode = MovementMode(movement_mode)
        except ValueError as error:
            allowed = ", ".join(mode.value for mode in MovementMode)
            raise ToolError(f"movement_mode must be one of: {allowed}") from error
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
        set_open=set_open,
        to_level=to_level,
        movement_mode=selected_mode,
        as_bonus_action=as_bonus_action,
    )
    try:
        events = session.encounter.act(action, session.rng)
    except EncounterError as error:
        raise ToolError(str(error)) from error
    completed_at = _utc_now()
    session.event_timestamps.extend([completed_at] * len(events))
    _capture_checkpoint(session, completed_at)
    return {
        "events": [event.as_dict() for event in events],
        "state": session.encounter.state(),
    }


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
    set_open: bool | None = None,
    to_level: int | None = None,
    movement_mode: str | None = None,
    as_bonus_action: bool = False,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Take the current creature's action and durably audit success or refusal.

    ``request_id`` makes retries idempotent. The action fields have the same
    meanings documented by encounter_state and encounter_log: attacks name a
    target, movement names a destination/path/storey, and spells name their aim.
    """
    session = _session(encounter_id)
    cached = _cached_request(session, request_id)
    if cached is not None:
        return cached
    arguments: dict[str, Any] = {
        "kind": kind,
        "target": target,
        "attack": attack,
        "item": item,
        "spell": spell,
        "slot_level": slot_level,
        "to_position": to_position,
        "targets": targets,
        "center": center,
        "direction": direction,
        "toward": toward,
        "path": path,
        "feature": feature,
        "set_open": set_open,
        "to_level": to_level,
        "movement_mode": movement_mode,
        "as_bonus_action": as_bonus_action,
    }
    index, started_at = _attempt_started(
        encounter_id, session, "encounter_act", arguments, request_id
    )
    try:
        if session.finalized:
            raise ToolError(f"encounter {encounter_id!r} is finalized")
        result = _execute_encounter_act(
            encounter_id,
            kind,
            target,
            attack,
            item,
            spell,
            slot_level,
            to_position,
            targets,
            center,
            direction,
            toward,
            path,
            feature,
            set_open,
            to_level,
            movement_mode,
            as_bonus_action,
        )
    except (ToolError, EncounterError) as error:
        _attempt_finished(
            encounter_id,
            session,
            index=index,
            started_at=started_at,
            operation="encounter_act",
            arguments=arguments,
            request_id=request_id,
            status="refused",
            error=str(error),
        )
        raise ToolError(str(error)) from error
    _attempt_finished(
        encounter_id,
        session,
        index=index,
        started_at=started_at,
        operation="encounter_act",
        arguments=arguments,
        request_id=request_id,
        status="success",
        result=result,
    )
    return result


def _execute_encounter_advance(encounter_id: str) -> dict[str, Any]:
    """End the current turn and begin the next, rolling any death saves that are due."""
    session = _session(encounter_id)
    events = session.encounter.advance(session.rng)
    completed_at = _utc_now()
    session.event_timestamps.extend([completed_at] * len(events))
    _capture_checkpoint(session, completed_at)
    return {
        "events": [event.as_dict() for event in events],
        "state": session.encounter.state(),
    }


@server.tool()
def encounter_advance(
    encounter_id: str, request_id: str | None = None
) -> dict[str, Any]:
    """End this turn, begin the next, and durably record the transition."""
    session = _session(encounter_id)
    cached = _cached_request(session, request_id)
    if cached is not None:
        return cached
    arguments: dict[str, Any] = {}
    index, started_at = _attempt_started(
        encounter_id, session, "encounter_advance", arguments, request_id
    )
    try:
        if session.finalized:
            raise ToolError(f"encounter {encounter_id!r} is finalized")
        result = _execute_encounter_advance(encounter_id)
    except (ToolError, EncounterError) as error:
        _attempt_finished(
            encounter_id,
            session,
            index=index,
            started_at=started_at,
            operation="encounter_advance",
            arguments=arguments,
            request_id=request_id,
            status="refused",
            error=str(error),
        )
        raise ToolError(str(error)) from error
    _attempt_finished(
        encounter_id,
        session,
        index=index,
        started_at=started_at,
        operation="encounter_advance",
        arguments=arguments,
        request_id=request_id,
        status="success",
        result=result,
    )
    return result


@server.tool()
def encounter_resume(encounter_id: str) -> dict[str, Any]:
    """Load an encounter from its verified journal, repairing a partial crash tail."""
    existing = _SESSIONS.get(encounter_id)
    warning: dict[str, str] | None = None
    recovered = existing is None
    session = existing
    if session is None:
        session, warning = _recover_session(encounter_id)
    result: dict[str, Any] = {
        "encounter_id": encounter_id,
        "recovered": recovered,
        "finalized": session.finalized,
        "state": encounter_state(encounter_id),
    }
    if warning is not None:
        result["recovery_warning"] = warning
    return result


@server.tool()
def encounter_list(status: str = "active") -> dict[str, Any]:
    """Discover durable encounters without loading them into process memory."""
    if status not in {"active", "finalized", "all"}:
        raise ToolError("status must be active, finalized, or all")
    entries: list[dict[str, Any]] = []
    for path in _journal_service.list_journals():
        encounter_id = path.stem
        try:
            records, _ = _journal_service.read(encounter_id)
        except _journal_service.JournalError as error:
            if status == "all":
                entries.append(
                    {
                        "encounter_id": encounter_id,
                        "status": "corrupt",
                        "problem": str(error),
                        "journal_path": str(path),
                    }
                )
            continue
        if not records:
            continue
        finalized = any(record.get("kind") == "finalized" for record in records)
        actual_status = "finalized" if finalized else "active"
        if status != "all" and status != actual_status:
            continue
        entries.append(
            {
                "encounter_id": encounter_id,
                "status": actual_status,
                "created_at": records[0].get("timestamp"),
                "updated_at": records[-1].get("timestamp"),
                "records": len(records),
                "journal_path": str(path),
            }
        )
    return {"status": status, "encounters": entries}


@server.tool()
def encounter_finalize(encounter_id: str) -> dict[str, Any]:
    """Atomically export replay v2 and mark the durable encounter finalized."""
    session = _session(encounter_id)
    if session.finalization_result is not None:
        return deepcopy(session.finalization_result)
    target = _journal_service.encounters_root() / f"{encounter_id}.replay.json"
    exported = replay_export(
        encounter_id, path=str(target), format_version=_replay_service.LATEST_FORMAT_VERSION
    )
    result = {
        "encounter_id": encounter_id,
        "status": "finalized",
        "replay_path": str(target),
        "bytes": exported["bytes"],
        "sha256": exported["sha256"],
        "journal_path": str(_journal_service.journal_path(encounter_id)),
    }
    _journal_append(
        encounter_id,
        {"kind": "finalized", "timestamp": _utc_now(), "result": result},
    )
    session.finalized = True
    session.finalization_result = deepcopy(result)
    return result


#: A serialized replay bundle at or under this many bytes is returned inline;
#: a larger one is written to disk and answered with its path — the same
#: result-size rule the map tools follow for oversized documents.
_INLINE_BUNDLE_BYTES = 64 * 1024


@server.tool()
def replay_validate(bundle: dict[str, Any]) -> dict[str, Any]:
    """Validate a v1 or v2 replay and verify every v2 integrity hash."""
    diagnostics = _replay_service.validate_replay(bundle)
    return {
        "valid": not diagnostics,
        "error_count": len(diagnostics),
        "diagnostics": diagnostics,
    }


@server.tool()
def replay_export(
    encounter_id: str,
    path: str | None = None,
    embed: bool = False,
    format_version: int = _replay_service.LATEST_FORMAT_VERSION,
) -> dict[str, Any]:
    """Export a fight's replay: a bundle file, or a standalone viewer page.

    Version 2 is the default: a self-contained, validated audit record with
    normalized combatants, captured content and map, actions, attempts,
    timestamped events, authoritative state checkpoints, and integrity hashes.
    Pass ``format_version=1`` for the legacy viewer contract.

    Plain export: a small bundle is returned inline as ``bundle``; a large
    one — or any call with ``path`` — is written to disk (default
    ``<maps root>/replays/<name>-<seed>.json``) and answered with ``path``,
    ``bytes``, and ``sha256``. With ``embed`` true the bundle is baked into
    the replay viewer page instead, producing one self-contained ``.html``
    the user opens directly in a browser — no server, hand the file over.
    An existing file at the target is replaced: the export is derived from
    the session, not an original.
    """
    session = _session(encounter_id)
    if format_version == 1:
        name = (
            str(session.map_payload["name"])
            if session.map_payload is not None
            else encounter_id
        )
        bundle = _replay_service.replay_bundle(
            name=name,
            seed=session.seed,
            map_payload=session.map_payload,
            initial_creatures=session.initial_creatures,
            map_open_features=session.initial_open_features,
            events=[event.as_dict() for event in session.encounter.log],
        )
    elif format_version == 2:
        captured_map = session.map_payload or session.inline_map_payload
        name = str(captured_map["name"]) if captured_map is not None else encounter_id
        latest_state = session.encounter.state()
        latest_state["map_source"] = _map_source_of(session)
        initial_state = deepcopy(session.initial_state)
        initial_state["map_source"] = _map_source_of(session)
        checkpoints = []
        for index, captured_state in enumerate(session.state_history):
            checkpoint_state = deepcopy(captured_state)
            checkpoint_state["map_source"] = _map_source_of(session)
            checkpoints.append(
                {
                    "index": index,
                    "timestamp": session.checkpoint_timestamps[index],
                    "event_count": session.checkpoint_event_counts[index],
                    "state_hash": _replay_service.canonical_sha256(checkpoint_state),
                    "state": checkpoint_state,
                }
            )
        bundle = _replay_service.replay_bundle_v2(
            name=name,
            engine_version=__version__,
            encounter_id=encounter_id,
            seed=session.seed,
            movement_rule=session.encounter.movement_rule.value,
            map_payload=captured_map,
            initial_creatures=initial_state["combatants"],
            normalized_combatants=session.normalized_combatants,
            initial_state=initial_state,
            map_open_features=session.initial_open_features,
            actions=[record.as_dict() for record in session.encounter.actions],
            events=[event.as_dict() for event in session.encounter.log],
            event_timestamps=session.event_timestamps,
            latest_state=latest_state,
            checkpoints=checkpoints,
            attempts=session.attempts,
            content_snapshot=session.content_snapshot,
        )
    else:
        raise ToolError(f"format_version must be 1 or 2, got {format_version}")
    serialized = _replay_service.serialize_bundle(bundle)
    slug = slugify(name)
    result: dict[str, Any] = {
        "encounter_id": encounter_id,
        "seed": session.seed,
        "format": _replay_service.FORMAT,
        "events": len(session.encounter.log),
        "sha256": _replay_service.sha256_bytes(serialized.encode("utf-8")),
    }

    if embed:
        static = resources.files("fivee_sim.editor") / "static"
        viewer = (static / "viewer.html").read_text(encoding="utf-8")
        renderer = (static / "renderer.js").read_text(encoding="utf-8")
        html = _replay_service.embed_in_viewer(
            viewer, serialized, renderer_js=renderer
        )
        target = (
            Path(path).expanduser()
            if path is not None
            else _map_service.maps_root() / "replays" / f"{slug}-{session.seed}.html"
        )
        try:
            _replay_service.atomic_write_text(target, html)
        except OSError as error:
            raise ToolError(f"cannot write {target}: {error}") from error
        return {
            **result,
            "path": str(target),
            "bytes": len(html.encode("utf-8")),
            "sha256": _replay_service.sha256_bytes(html.encode("utf-8")),
        }

    size = len(serialized.encode("utf-8"))
    if path is None and size <= _INLINE_BUNDLE_BYTES:
        return {**result, "bundle": bundle, "bytes": size}
    target = (
        Path(path).expanduser()
        if path is not None
        else _map_service.maps_root() / "replays" / f"{slug}-{session.seed}.json"
    )
    try:
        _replay_service.atomic_write_text(target, serialized)
    except OSError as error:
        raise ToolError(f"cannot write {target}: {error}") from error
    return {
        **result,
        "path": str(target),
        "bytes": size,
        "sha256": _replay_service.sha256_bytes(serialized.encode("utf-8")),
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


def _changed_squares(
    before: MapLevel,
    after: MapLevel,
    before_legend: Mapping[str, str],
    after_legend: Mapping[str, str],
) -> list[Square]:
    """Every square one edit moved on one storey, however it moved it.

    Terrain is compared by the kind each document's own legend resolves, so a
    legend rewrite that leaves the tiles reading the same is no change and one
    that repoints a glyph is a change everywhere it appears. Heights count as
    much as tiles: an elevation op contributed nothing here, which is how an
    edit that only raised ground fell through to rendering the whole map.
    """
    squares: list[Square] = []
    legends_match = dict(before_legend) == dict(after_legend)
    for yy, (old_row, new_row) in enumerate(zip(before.tiles, after.tiles, strict=True)):
        if legends_match and old_row == new_row:
            continue
        for xx, (old_char, new_char) in enumerate(zip(old_row, new_row, strict=True)):
            if before_legend[old_char] != after_legend[new_char]:
                squares.append((xx, yy))
    olds = {feature.id: feature for feature in before.features}
    news = {feature.id: feature for feature in after.features}
    for feature_id in set(olds) | set(news):
        old, new = olds.get(feature_id), news.get(feature_id)
        if old == new:
            continue
        for feature in (old, new):
            if feature is not None:
                squares.append(feature.at)
    for square in set(before.elevation.squares) | set(after.elevation.squares):
        if before.elevation.at(square) != after.elevation.at(square):
            squares.append(square)
    return squares


def _edit_render(before: MapDocument, after: MapDocument) -> dict[str, Any]:
    """A render sized to what an edit touched, on the storey it touched.

    Every storey is diffed, and the lowest one that moved is the one drawn —
    reading the ground alone showed an unchanged ground floor after an edit
    carrying ``level: 1``, and did it without scanning at all on a map small
    enough to inline. An edit that moved no square draws the ground.

    The whole storey when the map is small enough to inline; otherwise the
    bounding box of every square that changed on it, downsampled just far
    enough to fit the render budget.
    """
    width, height = after.grid.width, after.grid.height
    touched: dict[int, list[Square]] = {}
    # A resize moves every storey and there is no smaller thing to show; it
    # also makes the row-by-row diff below ill-shaped, so it never runs.
    resized = (before.grid.width, before.grid.height) != (width, height)
    if not resized:
        for index in sorted(after.levels):
            old = before.levels.get(index)
            if old is None:  # a storey the edit added: all of it is new
                touched[index] = []
                continue
            new = after.levels[index]
            squares = _changed_squares(old, new, before.legend, after.legend)
            if squares or old.elevation.default != new.elevation.default:
                touched[index] = squares
    level = min(touched) if touched else GROUND_LEVEL
    if width * height <= _INLINE_RENDER_CELLS:
        return _map_service.render_ascii(after, level=level)
    changed = touched.get(level) or []
    if changed:
        xs = [square[0] for square in changed]
        ys = [square[1] for square in changed]
    else:
        xs, ys = [0, width - 1], [0, height - 1]
    x0, y0 = min(xs), min(ys)
    box_w, box_h = max(xs) - x0 + 1, max(ys) - y0 + 1
    downsample = 1
    while -(-box_w // downsample) * -(-box_h // downsample) > _map_service.RENDER_BUDGET:
        downsample += 1
    return _map_service.render_ascii(
        after, x=x0, y=y0, width=box_w, height=box_h, downsample=downsample, level=level
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
    show_elevation: bool = False,
    level: int = 0,
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

    ``encounter_id`` also shows the map *that fight is on* rather than the map
    as authored: a fixture the fight has opened draws open, floods whatever its
    overlay governs, and drops that ground with it. A terrain kind a fixture
    introduces that the document's legend has no glyph for borrows one, and
    ``legend`` names what it borrowed like any other glyph. Without an
    ``encounter_id`` the render is the file on disk, fixtures included.

    ``show_elevation`` adds ``elevation_rows`` and ``elevation_legend`` beside
    the terrain rows: one glyph per square, lettered from the lowest ground in
    view upward, with the legend giving each its height in feet.
    """
    session = _map_session(map_id)
    tokens: dict[Square, str] = {}
    letters: dict[str, str] = {}
    open_features: list[str] | None = None
    if encounter_id is not None:
        tokens, letters = _encounter_tokens(session.document, encounter_id)
        # The fight's live fixture states. A mapless fight has none, and a fight
        # on some other map contributes names this document simply does not
        # have — the same leniency the token overlay already takes with a
        # position that lands off the map.
        map_state = _session(encounter_id).encounter.map_state
        if map_state is not None:
            open_features = sorted(map_state.open_features)
    try:
        rendered = _map_service.render_ascii(
            session.document,
            x=x, y=y, width=width, height=height,
            downsample=downsample, show_features=show_features,
            show_elevation=show_elevation, level=level,
            tokens=tokens or None, open=open_features,
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
    horizontal_first?}, ``add_feature`` {feature}, ``set_feature`` {feature} to
    edit one in place by the id in its record — it keeps the feature's position
    in the array and the storey it stands on, and **writes the record whole**, so
    a key left out is a key removed rather than kept — ``remove_feature`` {id},
    ``toggle_door`` {at}, ``resize`` {width, height, anchor?, fill?},
    ``set_legend`` {glyph, terrain}, ``set_name`` {name},
    ``set_palette`` {terrain, color} to color a terrain kind in this document —
    one hex color, a {light, dark} pair of them, or null to drop back to the
    color the renderers compute —
    ``set_elevation`` {rect | cells, feet} or {default} to move the height every
    unnamed square sits at, ``adjust_elevation`` {rect | cells, by} to raise or
    lower what is already there. Heights are feet and may be negative.

    The ``feature`` both feature ops take is {id, kind, at, orientation?,
    hinge?, swing?, state?, linked_to?, team?, to_level?} plus, for a fixture,
    terrain, elevation, affects, requires, costs_action and check. A door's
    hinge and swing use the cardinal directions valid for its orientation.
    ``linked_to`` must name one reciprocal adjacent door with the same state and
    interaction contract; toggling either leaf toggles both. ``to_level`` makes the feature a
    connector — the square a creature may step between storeys on, which is what
    turns a drawn stairway into a walkable one.

    A bad operation is refused with its index and changes nothing. A successful edit bumps the
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
    level: int = 0,
) -> dict[str, Any]:
    """Geometry over a loaded map: "distance", "line_of_sight", or "path".

    ``frm`` and ``to`` are ``[x, y]`` square indices (``frm`` because ``from``
    is a reserved word in the implementation language). Doors count in their
    recorded default state and nothing is occupied — for questions inside a
    fight, use the encounter tools, which see live doors and creatures.
    ``distance`` answers in feet; ``line_of_sight`` is a boolean; ``path``
    returns the squares and cost in feet, or ``reachable: false``. Ground height
    is charged to a ``path`` — a slope is difficult terrain and a cliff is a
    climb, and the result names both ends' elevation so a large cost is
    explainable — but ``distance`` and ``line_of_sight`` are measured flat.
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
            terrain=_registry().terrain_effects, level=level,
        )
    except (UnknownTerrain, ValueError) as error:
        raise ToolError(str(error)) from error
    return {"map_id": map_id, **answer}


@server.tool()
def uvtt_export(
    map_id: str,
    path: str | None = None,
    pixels_per_grid: int = 32,
    include_image: bool = True,
    level: int = 0,
    open_features: list[str] | None = None,
) -> dict[str, Any]:
    """Export a loaded map as a Universal VTT file another virtual tabletop can import.

    The payload carries wall polylines derived from the terrain, one portal per
    door feature (with its recorded default open/closed state), and — unless
    ``include_image`` is false — a rendered PNG of the map, which some
    importers require. Lights, object line-of-sight, and elevation are
    deliberately absent: the engine does not model them. The format has one
    plane, so ``level`` picks the storey to export and a map with floors takes
    one call per floor. The image side is
    capped at 4096 pixels; lower ``pixels_per_grid`` for large maps.

    ``open_features`` names the fixtures to export as open — a fight's live
    set, which ``encounter_state``'s map block reports. Given it, the walls,
    the image and the portals all show the map *that fight is on*: a raised
    portcullis stops being a wall and a sluice's flooded room exports as water.
    Omit it and the export is the map as the file has it. A door's own square is
    the one thing that does not change either way: a door travels as a portal
    here, and a portal in solid wall is a door the importer cannot open.

    The result is always written to disk — default
    ``<maps root>/uvtt/<slug-of-name>.uvtt`` — never inlined, because the
    payload embeds a base64 image. An existing file at the target is
    replaced: the export is derived from the session's map, not an original.
    """
    session = _map_session(map_id)
    try:
        payload = _uvtt_service.to_uvtt(
            session.document,
            terrain=_registry().terrain_effects,
            pixels_per_grid=pixels_per_grid,
            include_image=include_image,
            level=level,
            open=open_features,
        )
    except (UnknownTerrain, ValueError) as error:
        raise ToolError(str(error)) from error
    target = (
        Path(path).expanduser()
        if path is not None
        else _map_service.maps_root() / "uvtt" / f"{slugify(session.document.name)}.uvtt"
    )
    text = json.dumps(payload) + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as error:
        raise ToolError(f"cannot write {target}: {error}") from error
    return {
        "path": str(target),
        "bytes": len(text.encode("utf-8")),
        "map_id": map_id,
        "resolution": payload["resolution"],
        "wall_polylines": len(payload["line_of_sight"]),
        "portals": len(payload["portals"]),
        "image": include_image,
    }


# --- the interactive editor ------------------------------------------------
#: How long map_editor_serve waits for a spawned editor to bind and report.
_EDITOR_SPAWN_TIMEOUT = 5.0
#: Spawned editor processes, kept so the parent can reap them once stopped.
_EDITOR_PROCESSES: list[subprocess.Popen[bytes]] = []


def _editor_maps_dir(maps_dir: str | None) -> Path:
    return Path(maps_dir).expanduser() if maps_dir is not None else _map_service.maps_root()


def _editor_ping(port: int, token: str) -> dict[str, Any] | None:
    """The editor's ``/api/ping`` answer, or ``None`` when nothing answers."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/ping", headers={TOKEN_HEADER: token}
    )
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _live_editor_state(state_path: Path) -> dict[str, Any] | None:
    """The state file's record, but only when the server it names still answers."""
    state = read_state(state_path)
    if state is None:
        return None
    port, token = state.get("port"), state.get("token")
    if not isinstance(port, int) or not isinstance(token, str):
        return None
    if _editor_ping(port, token) is None:
        return None
    return state


@server.tool()
def map_editor_serve(port: int | None = None, maps_dir: str | None = None) -> dict[str, Any]:
    """Start the browser map editor and report its URL — or find it already up.

    The editor is a separate localhost-only process serving the maps
    directory; hand its ``url`` to the user to open in a browser. The served
    page configures its own access token, so the URL alone is enough. Calling
    this again while the editor runs returns the same URL with
    ``already_running`` true rather than starting a second one. After the user
    saves in the editor, the file is the truth — ``map_load`` (with
    ``replace``) re-reads it into the session. ``maps_dir`` defaults to the
    configured maps root; ``port`` defaults to an ephemeral one.
    """
    root = _editor_maps_dir(maps_dir)
    state_path = state_file_for(root)
    live = _live_editor_state(state_path)
    if live is not None:
        return {
            "url": f"http://127.0.0.1:{live['port']}/",
            "port": live["port"],
            "maps_dir": str(live.get("maps_dir", root)),
            "already_running": True,
        }
    # A state file nobody answers for describes a dead server; clear it so the
    # fresh spawn's record is the only one anybody can read.
    state_path.unlink(missing_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = state_path.parent / "editor.log"
    arguments = [
        sys.executable, "-m", "fivee_sim.editor",
        "--maps-dir", str(root),
        "--state-file", str(state_path),
    ]
    if port is not None:
        arguments += ["--port", str(port)]
    # stdout must be the logfile, never inherited: this process's stdout is
    # the JSON-RPC channel, and one stray line on it breaks the protocol.
    with open(log_path, "ab") as log_file:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    _EDITOR_PROCESSES.append(process)
    deadline = time.monotonic() + _EDITOR_SPAWN_TIMEOUT
    state: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        candidate = read_state(state_path)
        if candidate is not None and isinstance(candidate.get("port"), int):
            state = candidate
            break
        if process.poll() is not None:
            raise ToolError(
                f"the editor process exited with status {process.returncode} before "
                f"binding; see the log at {log_path}"
            )
        time.sleep(0.05)
    if state is None:
        process.terminate()
        raise ToolError(
            f"the editor did not report a bound port within "
            f"{_EDITOR_SPAWN_TIMEOUT:.0f}s; see the log at {log_path}"
        )
    return {
        "url": f"http://127.0.0.1:{state['port']}/",
        "port": state["port"],
        "maps_dir": str(root),
        "already_running": False,
        "log": str(log_path),
    }


@server.tool()
def map_editor_stop(maps_dir: str | None = None) -> dict[str, Any]:
    """Stop the browser map editor for a maps directory, if one is running.

    Asks it to shut down gracefully over its own API, falls back to SIGTERM
    at the recorded pid, and clears the state file either way. ``was_running``
    reports whether anything was there to stop.
    """
    root = _editor_maps_dir(maps_dir)
    state_path = state_file_for(root)
    state = read_state(state_path)
    if state is None:
        return {"stopped": False, "was_running": False}
    port, token, pid = state.get("port"), state.get("token"), state.get("pid")
    stopped = False
    if isinstance(port, int) and isinstance(token, str):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/shutdown",
            method="POST",
            headers={TOKEN_HEADER: token},
            data=b"",
        )
        try:
            with urllib.request.urlopen(request, timeout=2.0):
                stopped = True
        except (OSError, ValueError):
            stopped = False
    if not stopped and isinstance(pid, int):
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except OSError:
            stopped = False
    if stopped:
        # The exiting server removes its own state file; give it a moment so
        # the record disappears with the process rather than being yanked
        # from under it.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and state_path.exists():
            time.sleep(0.05)
    state_path.unlink(missing_ok=True)
    for process in _EDITOR_PROCESSES:
        if process.pid == pid and process.poll() is None:
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                pass
    # A state file existed, so something was there to stop — even when both
    # shutdown paths failed because the recorded process is already dead.
    return {"stopped": stopped, "was_running": True}


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
