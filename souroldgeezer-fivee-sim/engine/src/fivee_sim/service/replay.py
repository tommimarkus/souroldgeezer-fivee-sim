"""Replay bundles: one fight as one portable, verifiable JSON document.

Version 2 is the default service contract. It is self-contained: normalized
starting combatants, captured map and content, successful actions, all audited
attempts, timestamped events, authoritative state checkpoints, and component
hashes. Version 1 remains available byte-for-shape for legacy consumers::

    {
      "format": "fivee-sim-replay",
      "format_version": 1,
      "name": "...",                 # the map's name, or the encounter id
      "seed": 123,                   # the encounter's seed
      "map": {...} | null,           # a fivee-sim-map payload, by value
      "initial": {
        "creatures": [{"name", "team", "position": [x_feet, y_feet],
                       "hp", "max_hp"}, ...],
        "map_open_features": ["door-1", ...]   # feature ids open at the start
      },
      "events": [Event.as_dict(), ...]
    }

In either version a loaded ``map`` is captured by value. Legacy v1 keeps its
documented neutral-plane treatment of inline map specs; v2 converts the runtime
map into a complete map document, including storeys.

:func:`embed_in_viewer` turns the viewer page plus a bundle into a single
self-contained HTML file: the bundle lands in the page's embedded-data slot,
and the shared renderer can be inlined so the file works over ``file://``
with no server and no sibling assets.

**An adventure's replay is a second format, not a third version of this one.**
:func:`validate_replay`'s version-agnostic prefix demands ``seed``,
``initial.creatures``, ``initial.map_open_features``, ``map`` and ``events``
*before* it reaches the v1 early return, and an envelope carrying whole fights
as chapters has none of them — so ``format_version: 3`` would be a document that
fails its own validator on every field. :data:`ADVENTURE_FORMAT` says what it is
instead, :func:`adventure_replay_bundle` composes one and
:func:`validate_adventure_replay` grades it.

Both halves of that format live here, beside the format they nest, for two
reasons. One is the recurring defect this feature kept producing: a composer and
a validator in different modules are two declarations that must agree, and
:data:`ADVENTURE_INTEGRITY_KEYS` is the single one they both read. The other is
a hard import constraint — ``service/encounters.py`` imports ``map_ops``, so
``map_ops`` cannot import ``service/adventures.py``, and the dispatch in
``map_ops.replay_validate`` has to reach the envelope validator through a module
it already has.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..kernel.grid import as_point
from ..kernel.rules import Ability
from ..model.battlemap import BattleMap, FeatureOverlay, MapFeature, MapPlane
from ..model.creature import AttackOption, Creature
from ..paths import (
    REPLAYS_ENV,
    REPLAYS_SUBDIR,
    environment_replay_roots,
    replays_root,
)
from .common import discover_json_files
from .errors import ReplayError

__all__ = [
    "ADVENTURE_FORMAT",
    "ADVENTURE_FORMAT_VERSION",
    "ADVENTURE_INTEGRITY_KEYS",
    "EMBED_SLOT",
    "FORMAT",
    "FORMAT_VERSION",
    "LATEST_FORMAT_VERSION",
    "RENDERER_TAG",
    "REPLAYS_ENV",
    "REPLAYS_SUBDIR",
    "adventure_replay_bundle",
    "atomic_write_text",
    "battle_map_payload",
    "canonical_sha256",
    "embed_in_viewer",
    "environment_replay_roots",
    "list_replays",
    "load_bundle_file",
    "normalized_combatant_payload",
    "replay_bundle",
    "replay_bundle_v2",
    "replays_root",
    "serialize_bundle",
    "sha256_bytes",
    "validate_adventure_replay",
    "validate_replay",
]

FORMAT = "fivee-sim-replay"
FORMAT_VERSION = 1
LATEST_FORMAT_VERSION = 2

#: What an adventure's composed replay says it is. A distinct discriminator
#: rather than a third ``format_version``, for the reason in the module
#: docstring, and what ``map_ops.replay_validate`` dispatches on.
ADVENTURE_FORMAT = "fivee-sim-adventure-replay"
ADVENTURE_FORMAT_VERSION = 1

#: The envelope blocks the integrity hashes cover, read by the composer and by
#: the validator so the two cannot cover different sets. Everything else in an
#: envelope is metadata: the discriminator (which must not hash itself, or a
#: document renamed to another format would still verify), the engine version
#: that wrote it, and the block of hashes itself.
ADVENTURE_INTEGRITY_KEYS: tuple[str, ...] = ("adventure", "chapters")

#: The exact slot the viewer page carries for embedded data. ``test_web_assets``
#: pins that the page contains it exactly once, exactly like this.
EMBED_SLOT = '<script type="application/json" id="embedded-data">null</script>'
#: The exact tag the viewer loads the shared renderer with, also pinned by
#: test — replacing it inlines the renderer for a standalone file.
RENDERER_TAG = '<script src="/assets/renderer.js"></script>'


def _expand_exponent(value: str) -> str:
    mantissa, raw_exponent = value.lower().split("e", 1)
    exponent = int(raw_exponent)
    sign = ""
    if mantissa.startswith("-"):
        sign, mantissa = "-", mantissa[1:]
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    decimal_at = len(whole) + exponent
    if decimal_at <= 0:
        return sign + "0." + ("0" * -decimal_at) + digits
    if decimal_at >= len(digits):
        return sign + digits + ("0" * (decimal_at - len(digits)))
    return sign + digits[:decimal_at] + "." + digits[decimal_at:]


def _javascript_number(value: int | float) -> str:
    """Spell a finite JSON number the way ``JSON.stringify`` does."""
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ValueError("canonical JSON does not support non-finite numbers")
    if value == 0:
        return "0"
    rendered = repr(value).lower()
    magnitude = abs(value)
    if 1e-6 <= magnitude < 1e21:
        if "e" in rendered:
            return _expand_exponent(rendered)
        if value.is_integer():
            return str(int(value))
        return rendered
    mantissa, raw_exponent = rendered.split("e", 1)
    exponent = int(raw_exponent)
    return f"{mantissa}e{'+' if exponent >= 0 else ''}{exponent}"


def _json_key(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _javascript_number(value)
    raise TypeError(f"canonical JSON object key must be scalar, got {type(value)!r}")


def _canonical_json(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _javascript_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, Mapping):
        items = [(_json_key(key), item) for key, item in value.items()]
        items.sort(key=lambda pair: pair[0])
        return "{" + ",".join(
            f"{json.dumps(key, ensure_ascii=False)}:{_canonical_json(item)}"
            for key, item in items
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    raise TypeError(f"value is not JSON serializable: {type(value)!r}")


def _canonical_bytes(value: Any) -> bytes:
    return _canonical_json(value).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Stable SHA-256 of a JSON value, independent of mapping insertion order."""
    return sha256(_canonical_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    """SHA-256 of exact file bytes, used for JSON and standalone HTML exports."""
    return sha256(value).hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    """Durably replace ``path`` with UTF-8 text using a same-directory temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise


def _attack_payload(option: AttackOption) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": option.name,
        "attack_bonus": option.attack_bonus,
        "damage": str(option.damage),
        "damage_type": option.damage_type.value,
        "kind": option.kind.value,
        "reach": option.reach,
        "normal_range": option.normal_range,
        "long_range": option.long_range,
        "provenance": option.provenance,
    }
    for name in ("bonus_damage", "advantage_bonus_damage"):
        value = getattr(option, name)
        if value is not None:
            payload[name] = str(value)
    if option.bonus_damage_type is not None:
        payload["bonus_damage_type"] = option.bonus_damage_type.value
    if option.advantage_bonus_with_adjacent_ally:
        payload["advantage_bonus_with_adjacent_ally"] = True
    if option.on_hit_condition is not None:
        payload["on_hit_condition"] = option.on_hit_condition
    if option.on_hit_save_ability is not None:
        payload["on_hit_save_ability"] = option.on_hit_save_ability.value
        payload["on_hit_save_dc"] = option.on_hit_save_dc
    if option.on_hit_expiry.value != "none":
        payload["on_hit_expiry"] = option.on_hit_expiry.value
    if option.on_hit_max_size is not None:
        payload["on_hit_max_size"] = option.on_hit_max_size.value
    if option.on_hit_attach:
        payload["on_hit_attach"] = True
        assert option.attached_damage is not None
        assert option.attached_damage_type is not None
        payload["attached_damage"] = str(option.attached_damage)
        payload["attached_damage_type"] = option.attached_damage_type.value
        if option.detach_after_damage:
            payload["detach_after_damage"] = option.detach_after_damage
    if option.ammunition is not None:
        payload["ammunition"] = option.ammunition
    if option.loading:
        payload["loading"] = True
    if option.thrown:
        payload["thrown"] = True
    return payload


def normalized_combatant_payload(creature: Creature) -> dict[str, Any]:
    """Complete JSON-ready creation input for a captured combatant."""
    return {
        "name": creature.name,
        "team": creature.team,
        "ac": creature.ac,
        "max_hp": creature.max_hp,
        "hp": creature.hp,
        "speed": creature.speed,
        "climb_speed": creature.climb_speed,
        "swim_speed": creature.swim_speed,
        "fly_speed": creature.fly_speed,
        "burrow_speed": creature.burrow_speed,
        "terrain_cost_overrides": sorted(creature.terrain_cost_overrides),
        "darkvision": creature.darkvision,
        "blindsight": creature.blindsight,
        "tremorsense": creature.tremorsense,
        "truesight": creature.truesight,
        "death_rule": creature.death_rule.value,
        "size": creature.size.value,
        "abilities": {
            ability.value: creature.abilities.get(ability, 10) for ability in Ability
        },
        "save_bonuses": {
            ability.value: bonus
            for ability, bonus in sorted(
                creature.save_bonuses.items(), key=lambda item: item[0].value
            )
        },
        "skill_bonuses": dict(sorted(creature.skill_bonuses.items())),
        "attacks": [_attack_payload(option) for option in creature.attacks],
        "attacks_per_action": creature.attacks_per_action,
        "bonus_actions": sorted(creature.bonus_actions),
        "surrender_when_last": creature.surrender_when_last,
        "redirect_attack": creature.redirect_attack,
        "pack_tactics": creature.pack_tactics,
        "undead_fortitude": creature.undead_fortitude,
        "spells": list(creature.spells),
        "spell_slots": dict(sorted(creature.spell_slots.items())),
        "spell_save_dc": creature.spell_save_dc,
        "spell_attack_bonus": creature.spell_attack_bonus,
        # Carried like the DC and the attack bonus beside it. A combatant that
        # arrived in the next fight without this would keep its healing spell
        # and quietly cast it for the flat dice.
        "spellcasting_ability": (
            creature.spellcasting_ability.value
            if creature.spellcasting_ability is not None
            else None
        ),
        # Carried like the fields above it: a combatant recovered into a new
        # fight without this would fall back to its Dexterity modifier even
        # when its stat block prints a different Initiative bonus.
        "initiative_bonus": creature.initiative_bonus,
        # Transcription-only, like the field above it, but consumed by
        # nothing: carried so a recovered combatant's sheet still reads
        # exactly as authored.
        "passive_perception": creature.passive_perception,
        "resistances": sorted(kind.value for kind in creature.resistances),
        "immunities": sorted(kind.value for kind in creature.immunities),
        "vulnerabilities": sorted(kind.value for kind in creature.vulnerabilities),
        "condition_immunities": sorted(creature.condition_immunities),
        "items": dict(sorted(creature.items.items())),
        "conditions": sorted(creature.conditions),
        # How the fight left them. Without these a recovered combatant comes
        # back on their feet: `dying` is derived as `not dead and hp == 0 and
        # not stable`, so dropping `stable` alone turns a stabilised creature
        # back into a dying one. `hp` above carries the 0; these carry what it
        # means. Shaped as the state payload reports them, which is the shape
        # ``parse_death_saves`` accepts.
        "death_saves": {
            "successes": creature.death_save_successes,
            "failures": creature.death_save_failures,
        },
        "stable": creature.stable,
        "dead": creature.dead,
        "surrendered": creature.surrendered,
        "position": list(as_point(creature.position)),
        "level": creature.level,
        "arrival_round": creature.arrival_round,
        # Emitted even when unset, unlike the state payload's conditional key.
        # This is creation input rather than a report: ``recover_session`` feeds
        # it back through ``combatants_from_specs``, and a key that is absent
        # here is one the rebuilt fight never hears about. ``parse_facing``
        # takes ``None`` for untracked, so the round trip is total either way.
        "facing": creature.facing,
        "provenance": creature.provenance,
    }


def replay_bundle(
    *,
    name: str,
    seed: int,
    map_payload: Mapping[str, Any] | None,
    initial_creatures: Sequence[Mapping[str, Any]],
    map_open_features: Sequence[str],
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compose a replay bundle from facts a session captured at creation.

    Every container is copied on the way in, so the bundle shares no mutable
    state with the running encounter that produced it.
    """
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "name": name,
        "seed": seed,
        "map": dict(map_payload) if map_payload is not None else None,
        "initial": {
            "creatures": [dict(creature) for creature in initial_creatures],
            "map_open_features": list(map_open_features),
        },
        "events": [dict(event) for event in events],
    }


def _terrain_glyphs(battle_map: BattleMap) -> dict[str, str]:
    kinds: set[str] = set()
    for plane in battle_map.levels.values():
        kinds.add(plane.default_terrain)
        kinds.update(plane.terrain.values())
        for feature in plane.features.values():
            kinds.update((feature.closed_terrain, feature.open_terrain))
            for overlay in feature.affects:
                if overlay.terrain is not None:
                    kinds.update((overlay.terrain.closed, overlay.terrain.open))
    pool = [
        char for char in ".#~,:;!?$&*abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        if char not in "+/<>@"
    ]
    glyphs: dict[str, str] = {}
    for index, kind in enumerate(sorted(kinds)):
        glyphs[kind] = pool[index] if index < len(pool) else chr(0xE000 + index)
    return glyphs


def _elevation_payload(plane: MapPlane) -> dict[str, Any] | None:
    squares = [
        [square[0], square[1], feet]
        for square, feet in sorted(
            plane.elevation.items(), key=lambda item: (item[0][1], item[0][0])
        )
        if feet != plane.default_elevation
    ]
    if plane.default_elevation == 0 and not squares:
        return None
    return {"default": plane.default_elevation, "squares": squares}


def _overlay_payload(overlay: FeatureOverlay) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cells": [[square[0], square[1]] for square in overlay.squares]
    }
    if overlay.terrain is not None:
        payload["terrain"] = {
            "closed": overlay.terrain.closed,
            "open": overlay.terrain.open,
        }
    if overlay.elevation is not None:
        payload["elevation"] = {
            "closed": overlay.elevation.closed,
            "open": overlay.elevation.open,
        }
    return payload


def _feature_payload(feature: MapFeature) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": feature.name,
        "kind": feature.kind,
        "at": [feature.square[0], feature.square[1]],
        "state": "open" if feature.initially_open else "closed",
        "terrain": {
            "closed": feature.closed_terrain,
            "open": feature.open_terrain,
        },
    }
    if feature.orientation is not None:
        payload["orientation"] = feature.orientation
    if feature.linked_to is not None:
        payload["linked_to"] = feature.linked_to
    if feature.elevation is not None:
        payload["elevation"] = {
            "closed": feature.elevation.closed,
            "open": feature.elevation.open,
        }
    if feature.affects:
        payload["affects"] = [_overlay_payload(overlay) for overlay in feature.affects]
    if feature.requires:
        payload["requires"] = list(feature.requires)
    if feature.trigger is not None:
        payload["trigger"] = {
            "when": {
                name: "open" if expected else "closed"
                for name, expected in feature.trigger.when
            },
            "set": "open" if feature.trigger.set_open else "closed",
            "mode": feature.trigger.mode.value,
        }
    if feature.costs_action:
        payload["costs_action"] = True
    if feature.check is not None:
        payload["check"] = {
            "ability": feature.check.ability.value,
            "dc": feature.check.dc,
        }
    return payload


def battle_map_payload(battle_map: BattleMap) -> dict[str, Any]:
    """A portable map-document payload for a caller-supplied battle map."""
    glyph_of = _terrain_glyphs(battle_map)
    legend = {glyph: kind for kind, glyph in glyph_of.items()}

    def level_payload(index: int, plane: MapPlane) -> dict[str, Any]:
        rows = []
        for y in range(battle_map.height):
            rows.append("".join(
                glyph_of[plane.terrain.get((x, y), plane.default_terrain)]
                for x in range(battle_map.width)
            ))
        payload: dict[str, Any] = {
            "tiles": rows,
            "features": [
                _feature_payload(feature)
                for _, feature in sorted(plane.features.items())
            ],
        }
        elevation = _elevation_payload(plane)
        if elevation is not None:
            payload["elevation"] = elevation
        if index != 0:
            payload["index"] = index
            payload["name"] = f"level {index}"
        return payload

    ground = level_payload(0, battle_map.levels[0])
    payload = {
        "format": "fivee-sim-map",
        "format_version": 1,
        "name": battle_map.name,
        "grid": {
            "width": battle_map.width,
            "height": battle_map.height,
            "cell_feet": 5,
        },
        "legend": legend,
        **ground,
        "provenance": {
            "generator": "inline",
            "seed": 0,
            "params": {},
            "edited": False,
            "source": "Caller-supplied inline map",
        },
    }
    levels = [
        level_payload(index, battle_map.levels[index])
        for index in sorted(battle_map.levels)
        if index != 0
    ]
    if levels:
        payload["levels"] = levels
    return payload


def replay_bundle_v2(
    *,
    name: str,
    engine_version: str,
    encounter_id: str,
    seed: int,
    movement_rule: str,
    map_payload: Mapping[str, Any] | None,
    initial_creatures: Sequence[Mapping[str, Any]],
    normalized_combatants: Sequence[Mapping[str, Any]],
    initial_state: Mapping[str, Any],
    map_open_features: Sequence[str],
    actions: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    event_timestamps: Sequence[str],
    latest_state: Mapping[str, Any],
    checkpoints: Sequence[Mapping[str, Any]],
    attempts: Sequence[Mapping[str, Any]],
    content_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Compose the additive, reconstructible replay-v2 envelope."""
    stamped_events = []
    for index, event in enumerate(events):
        stamped = dict(event)
        stamped["timestamp"] = (
            event_timestamps[index] if index < len(event_timestamps) else ""
        )
        stamped_events.append(stamped)
    content = dict(content_snapshot)
    content["sha256"] = canonical_sha256(content_snapshot)
    bundle: dict[str, Any] = {
        "format": FORMAT,
        "format_version": LATEST_FORMAT_VERSION,
        "name": name,
        "seed": seed,
        "map": dict(map_payload) if map_payload is not None else None,
        "initial": {
            "creatures": [dict(creature) for creature in initial_creatures],
            "combatants": [dict(creature) for creature in normalized_combatants],
            "map_open_features": list(map_open_features),
            "state": dict(initial_state),
        },
        "events": stamped_events,
        "engine_version": engine_version,
        "encounter": {
            "id": encounter_id,
            "seed": seed,
            "movement_rule": movement_rule,
        },
        "actions": [dict(action) for action in actions],
        "latest_state": dict(latest_state),
        "checkpoints": [dict(checkpoint) for checkpoint in checkpoints],
        "attempts": [dict(attempt) for attempt in attempts],
        "content": content,
    }
    bundle["integrity"] = {
        "algorithm": "sha256",
        "map": canonical_sha256(bundle["map"]),
        "initial": canonical_sha256(bundle["initial"]),
        "events": canonical_sha256(bundle["events"]),
        "actions": canonical_sha256(bundle["actions"]),
        "checkpoints": canonical_sha256(bundle["checkpoints"]),
        "latest_state": canonical_sha256(bundle["latest_state"]),
        "content": content["sha256"],
    }
    return bundle


def _diagnostic(path: str, problem: str) -> dict[str, str]:
    return {"path": path, "problem": problem}


def _is_json_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _validate_combatants(
    value: Any, path: str, found: list[dict[str, str]]
) -> None:
    if not isinstance(value, list):
        found.append(_diagnostic(path, "must be an array"))
        return
    for index, creature in enumerate(value):
        item_path = f"{path}.{index}"
        if not isinstance(creature, Mapping):
            found.append(_diagnostic(item_path, "must be an object"))
            continue
        if not isinstance(creature.get("name"), str) or not creature.get(
            "name", ""
        ).strip():
            found.append(_diagnostic(f"{item_path}.name", "must be a non-empty string"))
        position = creature.get("position")
        if (
            not isinstance(position, (list, tuple))
            or len(position) != 2
            or any(not _is_json_number(coordinate) for coordinate in position)
        ):
            found.append(
                _diagnostic(f"{item_path}.position", "must be two numeric coordinates")
            )
        for key in ("hp", "max_hp"):
            if not _is_json_number(creature.get(key)):
                found.append(_diagnostic(f"{item_path}.{key}", "must be numeric"))


def _validate_state(value: Any, path: str, found: list[dict[str, str]]) -> None:
    if not isinstance(value, Mapping):
        found.append(_diagnostic(path, "must be an object"))
        return
    if not isinstance(value.get("round"), int) or isinstance(value.get("round"), bool):
        found.append(_diagnostic(f"{path}.round", "must be an integer"))
    if not isinstance(value.get("turn"), str):
        found.append(_diagnostic(f"{path}.turn", "must be a string"))
    _validate_combatants(value.get("combatants"), f"{path}.combatants", found)


def _validate_map(value: Any, found: list[dict[str, str]]) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        found.append(_diagnostic("map", "must be an object or null"))
        return
    grid = value.get("grid")
    if not isinstance(grid, Mapping):
        found.append(_diagnostic("map.grid", "must be an object"))
    else:
        for key in ("width", "height", "cell_feet"):
            field = grid.get(key)
            if not isinstance(field, int) or isinstance(field, bool) or field <= 0:
                found.append(_diagnostic(f"map.grid.{key}", "must be a positive integer"))
    tiles = value.get("tiles")
    if not isinstance(tiles, list) or any(not isinstance(row, str) for row in tiles):
        found.append(_diagnostic("map.tiles", "must be an array of strings"))
    features = value.get("features")
    if not isinstance(features, list) or any(
        not isinstance(feature, Mapping) for feature in features
    ):
        found.append(_diagnostic("map.features", "must be an array of objects"))
    levels = value.get("levels", [])
    if not isinstance(levels, list):
        found.append(_diagnostic("map.levels", "must be an array"))
    else:
        for index, level in enumerate(levels):
            level_path = f"map.levels.{index}"
            if not isinstance(level, Mapping):
                found.append(_diagnostic(level_path, "must be an object"))
                continue
            if not isinstance(level.get("index"), int) or isinstance(
                level.get("index"), bool
            ):
                found.append(_diagnostic(f"{level_path}.index", "must be an integer"))
            level_tiles = level.get("tiles")
            if not isinstance(level_tiles, list) or any(
                not isinstance(row, str) for row in level_tiles
            ):
                found.append(
                    _diagnostic(f"{level_path}.tiles", "must be an array of strings")
                )


def validate_replay(payload: Any) -> list[dict[str, str]]:
    """Validate a replay bundle and verify v2 component hashes.

    Diagnostics are deliberately small JSON objects so the HTTP adapter and the
    browser's shared invalid corpus can present the same paths and meanings.
    """
    found: list[dict[str, str]] = []
    if not isinstance(payload, Mapping):
        return [_diagnostic("$", "must be an object")]
    if payload.get("format") != FORMAT:
        found.append(_diagnostic("format", f"must be {FORMAT!r}"))
    version = payload.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool) or version not in (1, 2):
        found.append(_diagnostic("format_version", "must be 1 or 2"))
    seed = payload.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        found.append(_diagnostic("seed", "must be an integer"))
    elif not -(2**53 - 1) <= seed <= 2**53 - 1:
        found.append(_diagnostic("seed", "must be a JavaScript safe integer"))
    initial = payload.get("initial")
    if not isinstance(initial, Mapping):
        found.append(_diagnostic("initial", "must be an object"))
    else:
        _validate_combatants(initial.get("creatures"), "initial.creatures", found)
        if not isinstance(initial.get("map_open_features"), list):
            found.append(_diagnostic("initial.map_open_features", "must be an array"))
        elif any(not isinstance(item, str) for item in initial["map_open_features"]):
            found.append(
                _diagnostic("initial.map_open_features", "must contain only strings")
            )
    _validate_map(payload.get("map"), found)
    events = payload.get("events")
    if not isinstance(events, list):
        found.append(_diagnostic("events", "must be an array"))
    else:
        for index, event in enumerate(events):
            if not isinstance(event, Mapping):
                found.append(_diagnostic(f"events.{index}", "must be an object"))
            elif not isinstance(event.get("kind"), str) or not event.get(
                "kind", ""
            ).strip():
                found.append(
                    _diagnostic(f"events.{index}.kind", "must be a non-empty string")
                )
    if version != 2:
        return found

    encounter = payload.get("encounter")
    if not isinstance(encounter, Mapping):
        found.append(_diagnostic("encounter", "must be an object"))
    else:
        if not isinstance(encounter.get("id"), str) or not encounter.get("id", "").strip():
            found.append(_diagnostic("encounter.id", "must be a non-empty string"))
        encounter_seed = encounter.get("seed")
        if (
            not isinstance(encounter_seed, int)
            or isinstance(encounter_seed, bool)
            or not -(2**53 - 1) <= encounter_seed <= 2**53 - 1
        ):
            found.append(
                _diagnostic("encounter.seed", "must be a JavaScript safe integer")
            )
        elif encounter_seed != seed:
            found.append(_diagnostic("encounter.seed", "must equal seed"))
    for key in ("actions", "checkpoints", "attempts"):
        records = payload.get(key)
        if not isinstance(records, list):
            found.append(_diagnostic(key, "must be an array"))
        elif key != "checkpoints":
            for index, record in enumerate(records):
                if not isinstance(record, Mapping):
                    found.append(_diagnostic(f"{key}.{index}", "must be an object"))
    if isinstance(initial, Mapping):
        _validate_combatants(initial.get("combatants"), "initial.combatants", found)
        _validate_state(initial.get("state"), "initial.state", found)
    _validate_state(payload.get("latest_state"), "latest_state", found)
    if not isinstance(payload.get("content"), Mapping):
        found.append(_diagnostic("content", "must be an object"))
    if isinstance(events, list):
        for index, event in enumerate(events):
            if not isinstance(event, Mapping):
                continue
            if event.get("seq") != index:
                found.append(
                    _diagnostic(f"events.{index}.seq", f"must be {index}")
                )
            if not isinstance(event.get("timestamp"), str) or not event.get(
                "timestamp", ""
            ).strip():
                found.append(
                    _diagnostic(
                        f"events.{index}.timestamp", "must be a non-empty string"
                    )
                )
    checkpoints = payload.get("checkpoints")
    if isinstance(checkpoints, list):
        previous_count = -1
        for index, checkpoint in enumerate(checkpoints):
            if not isinstance(checkpoint, Mapping):
                found.append(
                    _diagnostic(f"checkpoints.{index}", "must be an object")
                )
                continue
            event_count = checkpoint.get("event_count")
            if (
                not isinstance(event_count, int)
                or isinstance(event_count, bool)
                or event_count < previous_count
                or (isinstance(events, list) and event_count > len(events))
            ):
                found.append(
                    _diagnostic(
                        f"checkpoints.{index}.event_count",
                        "must be monotonic and within the event log",
                    )
                )
            else:
                previous_count = event_count
            state = checkpoint.get("state")
            if not isinstance(state, Mapping):
                found.append(
                    _diagnostic(f"checkpoints.{index}.state", "must be an object")
                )
            else:
                _validate_state(state, f"checkpoints.{index}.state", found)
                try:
                    state_hash = canonical_sha256(state)
                except (TypeError, ValueError):
                    state_hash = None
                if state_hash is None:
                    found.append(
                        _diagnostic(
                            f"checkpoints.{index}.state_hash",
                            "state is not canonical JSON",
                        )
                    )
                elif checkpoint.get("state_hash") != state_hash:
                    found.append(
                        _diagnostic(
                            f"checkpoints.{index}.state_hash", "does not match state"
                        )
                    )
        if checkpoints and isinstance(checkpoints[-1], Mapping):
            if checkpoints[-1].get("state") != payload.get("latest_state"):
                found.append(
                    _diagnostic(
                        "latest_state", "must equal the final authoritative checkpoint"
                    )
                )
    integrity = payload.get("integrity")
    if not isinstance(integrity, Mapping):
        found.append(_diagnostic("integrity", "must be an object"))
        return found
    if integrity.get("algorithm") != "sha256":
        found.append(_diagnostic("integrity.algorithm", "must be 'sha256'"))
    for key in ("map", "initial", "events", "actions", "checkpoints", "latest_state"):
        try:
            actual_hash = canonical_sha256(payload.get(key))
        except (TypeError, ValueError):
            found.append(_diagnostic(f"integrity.{key}", f"{key} is not canonical JSON"))
            continue
        if integrity.get(key) != actual_hash:
            found.append(_diagnostic(f"integrity.{key}", f"does not match {key}"))
    content = payload.get("content")
    if isinstance(content, Mapping):
        unhashed = dict(content)
        recorded = unhashed.pop("sha256", None)
        try:
            actual = canonical_sha256(unhashed)
        except (TypeError, ValueError):
            found.append(
                _diagnostic("integrity.content", "content is not canonical JSON")
            )
            return found
        if recorded != actual or integrity.get("content") != actual:
            found.append(_diagnostic("integrity.content", "does not match content"))
    return found


def adventure_replay_bundle(
    *,
    engine_version: str,
    adventure: Mapping[str, Any],
    chapters: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compose one run's frozen fights into a single envelope.

    ``chapters`` are already-composed v2 bundles wearing the run's own record of
    each — index, encounter id, when it was linked, who was carried in. They are
    copied at the top level only: the nested bundle is the artifact that was
    frozen at finalization and is carried verbatim, because re-deriving it is
    exactly what an adventure replay must never do.

    The envelope's integrity block covers order and membership. Per-chapter
    integrity is already inside each chapter and stays :func:`validate_replay`'s
    job, which is why nothing here re-hashes a fight.
    """
    bundle: dict[str, Any] = {
        "format": ADVENTURE_FORMAT,
        "format_version": ADVENTURE_FORMAT_VERSION,
        "engine_version": engine_version,
        "adventure": dict(adventure),
        "chapters": [dict(chapter) for chapter in chapters],
    }
    bundle["integrity"] = {
        "algorithm": "sha256",
        **{key: canonical_sha256(bundle[key]) for key in ADVENTURE_INTEGRITY_KEYS},
    }
    return bundle


def _validate_chapters(value: Any, found: list[dict[str, str]]) -> None:
    """Order, membership, and each chapter's own bundle graded by its own rules."""
    if not isinstance(value, list):
        found.append(_diagnostic("chapters", "must be an array"))
        return
    if not value:
        found.append(_diagnostic("chapters", "must name at least one encounter"))
        return
    seen: dict[str, int] = {}
    for index, chapter in enumerate(value):
        at = f"chapters.{index}"
        if not isinstance(chapter, Mapping):
            found.append(_diagnostic(at, "must be an object"))
            continue
        # Position rather than uniqueness, the way a v2 event's ``seq`` is
        # checked: a chapter that is where it says it is cannot be duplicated
        # or reordered without one of the two disagreeing.
        if chapter.get("index") != index:
            found.append(_diagnostic(f"{at}.index", f"must be {index}"))
        encounter_id = chapter.get("encounter_id")
        if not isinstance(encounter_id, str) or not encounter_id.strip():
            found.append(
                _diagnostic(f"{at}.encounter_id", "must be a non-empty string")
            )
        elif encounter_id in seen:
            # Distinct from the index check: two chapters can be numbered 0 and
            # 1 and still be one fight, which makes the run's history disagree
            # with the party the carry-over says walked between them.
            found.append(
                _diagnostic(
                    f"{at}.encounter_id", f"repeats the fight in chapter {seen[encounter_id]}"
                )
            )
        else:
            seen[encounter_id] = index
        nested = chapter.get("replay")
        if not isinstance(nested, Mapping):
            found.append(_diagnostic(f"{at}.replay", "must be an object"))
            continue
        found.extend(
            _diagnostic(f"{at}.replay.{one['path']}", one["problem"])
            for one in validate_replay(nested)
        )


def validate_adventure_replay(payload: Any) -> list[dict[str, str]]:
    """Validate an adventure's composed replay, chapters included.

    A sibling of :func:`validate_replay` rather than a branch inside it: the two
    formats share no required field, and folding a second shape into a validator
    the browser's invalid corpus is also written against would make every one of
    those cases ambiguous.

    What this function checks is the *envelope* — that the chapters are in order,
    that no fight appears twice, and that the integrity block matches what it
    covers. Each chapter's own bundle is graded by :func:`validate_replay`, whose
    diagnostics are re-rooted under that chapter, so a broken fight is reported
    where it lives rather than as one opaque "chapter is invalid".
    """
    found: list[dict[str, str]] = []
    if not isinstance(payload, Mapping):
        return [_diagnostic("$", "must be an object")]
    if payload.get("format") != ADVENTURE_FORMAT:
        found.append(_diagnostic("format", f"must be {ADVENTURE_FORMAT!r}"))
    if payload.get("format_version") != ADVENTURE_FORMAT_VERSION:
        found.append(
            _diagnostic("format_version", f"must be {ADVENTURE_FORMAT_VERSION}")
        )
    adventure = payload.get("adventure")
    if not isinstance(adventure, Mapping):
        found.append(_diagnostic("adventure", "must be an object"))
    elif not isinstance(adventure.get("id"), str) or not adventure.get("id", "").strip():
        found.append(_diagnostic("adventure.id", "must be a non-empty string"))
    _validate_chapters(payload.get("chapters"), found)
    integrity = payload.get("integrity")
    if not isinstance(integrity, Mapping):
        found.append(_diagnostic("integrity", "must be an object"))
        return found
    if integrity.get("algorithm") != "sha256":
        found.append(_diagnostic("integrity.algorithm", "must be 'sha256'"))
    for key in ADVENTURE_INTEGRITY_KEYS:
        try:
            actual = canonical_sha256(payload.get(key))
        except (TypeError, ValueError):
            found.append(_diagnostic(f"integrity.{key}", f"{key} is not canonical JSON"))
            continue
        if integrity.get(key) != actual:
            found.append(_diagnostic(f"integrity.{key}", f"does not match {key}"))
    return found


def serialize_bundle(bundle: Mapping[str, Any]) -> str:
    """The bundle as one line of JSON plus a trailing newline."""
    return json.dumps(bundle, ensure_ascii=False) + "\n"


# --- reading replays off disk ------------------------------------------------
def list_replays(roots: Sequence[str | Path] | None = None) -> list[dict[str, Any]]:
    """Every replay bundle under the given (or configured) roots, briefly.

    Reads each file just far enough for a catalogue row, and deliberately does
    **not** validate: a listing's job is to show what is there, and a bundle
    whose hashes no longer verify still needs to appear so the user can see
    which file is the broken one. :func:`load_bundle_file` is where a bundle is
    graded. Files that are not replay bundles at all are skipped in silence,
    exactly as :func:`~fivee_sim.service.maps.list_maps` skips non-maps.
    """
    if roots is None:
        configured = environment_replay_roots()
        roots = configured if configured else [replays_root()]
    listed: list[dict[str, Any]] = []
    for path in discover_json_files(roots):
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("format") != FORMAT:
            continue
        events = payload.get("events")
        encounter = payload.get("encounter")
        listed.append(
            {
                "name": payload.get("name"),
                "path": str(path),
                "format_version": payload.get("format_version"),
                "seed": payload.get("seed"),
                "events": len(events) if isinstance(events, list) else 0,
                # v1 has no ``encounter`` block at all; a row reports ``None``
                # rather than omitting the key, so a caller never has to ask
                # which version it is holding before it can read the row.
                "encounter_id": (
                    encounter.get("id") if isinstance(encounter, dict) else None
                ),
                "sha256": sha256_bytes(raw),
            }
        )
    listed.sort(key=lambda entry: str(entry["path"]))
    return listed


def load_bundle_file(path: str | Path) -> dict[str, Any]:
    """One replay bundle, parsed and validated, or :class:`ReplayError`.

    The refusal carries :func:`validate_replay`'s own diagnostics, so a bundle
    rejected here is rejected in the same words ``replay_validate`` would use
    and an adapter has nothing to translate but the envelope.
    """
    target = Path(path).expanduser()
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ReplayError(f"{target} cannot be read: {error}") from None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ReplayError(f"{target} is not valid JSON: {error}") from None
    diagnostics = validate_replay(payload)
    if diagnostics:
        raise ReplayError(
            f"{target} is not a playable replay bundle: {len(diagnostics)} problem(s)",
            diagnostics,
        )
    assert isinstance(payload, dict)  # validate_replay refuses anything else
    return payload


def embed_in_viewer(
    viewer_html: str, bundle_json: str, *, renderer_js: str | None = None
) -> str:
    """Fill the viewer page's embedded-data slot with the bundle, exactly once.

    ``bundle_json`` is serialized bundle JSON; every ``<`` in it is re-escaped
    as ``\\u003c`` (valid JSON, byte-identical data) so no event's prose can
    smuggle a ``</script>`` into the page. With ``renderer_js`` given, the
    page's renderer reference is replaced by the script itself, making the
    result a single self-contained file that opens over ``file://``.
    """
    if viewer_html.count(EMBED_SLOT) != 1:
        raise ValueError(
            f"the viewer page must carry {EMBED_SLOT!r} exactly once; "
            f"found {viewer_html.count(EMBED_SLOT)}"
        )
    safe = bundle_json.strip().replace("<", "\\u003c")
    filled = viewer_html.replace(EMBED_SLOT, EMBED_SLOT.replace(">null<", f">{safe}<"), 1)
    if renderer_js is not None:
        if filled.count(RENDERER_TAG) != 1:
            raise ValueError(
                f"the viewer page must carry {RENDERER_TAG!r} exactly once; "
                f"found {filled.count(RENDERER_TAG)}"
            )
        filled = filled.replace(RENDERER_TAG, f"<script>\n{renderer_js}\n</script>", 1)
    return filled
