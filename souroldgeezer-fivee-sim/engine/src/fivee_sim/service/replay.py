"""Replay bundles: one fight as one portable, verifiable JSON document.

Version 3 is the default service contract. It is self-contained: normalized
starting combatants, captured map and content, successful actions, all audited
attempts, timestamped events, authoritative state checkpoints, and component
hashes. It differs from version 2 in one field: a checkpoint after the first
carries ``state_delta`` — what moved since the previous one — where v2 wrote
every state out whole, which for a fight of any length was over half the file.
Both remain writable for a caller who names one, and version 1 remains
available byte-for-shape for legacy consumers::

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
*before* it reaches :data:`ENVELOPED_FORMAT_VERSIONS`' early return, and an
envelope carrying whole fights
as chapters has none of them — so a run recorded as another ``format_version``
of this format would be a document that fails its own validator on every field.
That the number it would have taken has since been spent on a real fight version
is the argument arriving on time: a version counter says how one format changed,
never that a second format exists. :data:`ADVENTURE_FORMAT` says what it is
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

from ..kernel.grid import FEET_PER_SQUARE, as_point
from ..kernel.rules import Ability
from ..model.creature import AttackOption, Creature
from ..model.encounter import (
    STATE_ENTRIES,
    STATE_ROSTERS,
    EncounterMode,
    apply_state_delta,
    sheet_of,
    state_delta,
)
from ..paths import (
    REPLAYS_ENV,
    REPLAYS_SUBDIR,
    environment_replay_roots,
    replays_root,
)
from .common import canonical_json, discover_json_files, sha256_of
from .errors import ReplayError

__all__ = [
    "ADVENTURE_FORMAT",
    "ADVENTURE_FORMAT_VERSION",
    "ADVENTURE_INTEGRITY_KEYS",
    "CHAINED_FORMAT_VERSIONS",
    "EMBED_SLOT",
    "ENVELOPED_FORMAT_VERSIONS",
    "FORMAT",
    "FORMAT_VERSION",
    "LATEST_FORMAT_VERSION",
    "READABLE_FORMAT_VERSIONS",
    "RENDERER_TAG",
    "REPLAYS_ENV",
    "REPLAYS_SUBDIR",
    "adventure_replay_bundle",
    "atomic_write_text",
    "canonical_sha256",
    "embed_in_viewer",
    "environment_replay_roots",
    "list_replays",
    "load_bundle_file",
    "normalized_combatant_payload",
    "replay_bundle",
    "replay_bundle_v2",
    "replay_bundle_v3",
    "replays_root",
    "serialize_bundle",
    "sha256_bytes",
    "sheet_sha256",
    "validate_adventure_replay",
    "validate_replay",
]

FORMAT = "fivee-sim-replay"
FORMAT_VERSION = 1
LATEST_FORMAT_VERSION = 3

#: Every ``format_version`` :func:`validate_replay` accepts, read by the
#: validator instead of a bare literal so the two version constants above and
#: the reader cannot drift apart.
#:
#: A replay bundle is the one artifact in this codebase that leaves the
#: machine. A journal is internal state and gets a clean break —
#: ``journal_version`` bumps refuse the previous format outright, because
#: nothing outside the engine ever holds one. A bundle a user already
#: exported keeps sitting on their disk after the writer moves on, so the
#: engine defaults to :data:`LATEST_FORMAT_VERSION` and reads — and, for a
#: caller who names one, still writes — every version it has ever written.
#: ``map_ops.WRITABLE_FORMAT_VERSIONS`` is the producible set and is a
#: subset of this one, never the same declaration. ``LATEST_FORMAT_VERSION`` is
#: always a member of this set: that is the property that turns "the writer
#: moved and the reader forgot" into a failing test rather than a bundle the
#: engine writes and then refuses.
READABLE_FORMAT_VERSIONS: frozenset[int] = frozenset({1, 2, 3})

#: The readable versions carrying the full encounter envelope: everything
#: :data:`READABLE_FORMAT_VERSIONS` holds except the legacy
#: :data:`FORMAT_VERSION`, whose bundle is the bare shape documented above and
#: has no ``encounter``, no ``actions``, no ``attempts`` and no checkpoints.
#:
#: :func:`validate_replay` reads it rather than naming a version, and that is
#: the fix for a real defect: the gate was written as ``!= 2`` when 2 was the
#: only enveloped version, so version 3 — added by the phase that split
#: checkpoints into a keyframe and a chain of deltas — returned after the
#: version-agnostic prefix and every envelope check below was silently skipped.
#: A bundle with a missing ``state_delta``, a tampered chain or a
#: ``latest_state`` nothing reaches validated clean.
#:
#: A version this build does not read at all falls out here too, and
#: deliberately: grading an unknown shape against the newest envelope would
#: bury the one diagnostic that matters under twenty that do not.
ENVELOPED_FORMAT_VERSIONS: frozenset[int] = READABLE_FORMAT_VERSIONS - {FORMAT_VERSION}

#: The enveloped versions whose checkpoints are a keyframe and a chain of
#: deltas rather than a stack of whole states. Read by :func:`_checkpoint_state`
#: and mirrored in ``viewer.html``, so "does this bundle chain?" has one answer
#: on each side of the language boundary rather than a literal per reader.
#:
#: A set rather than a floor, because "3 and everything after it" is a claim
#: about versions nobody has designed. A v4 that keeps chaining joins this set
#: deliberately; one that does not, does not.
CHAINED_FORMAT_VERSIONS: frozenset[int] = frozenset({3})

#: The embedded map's own discriminator, mirrored from ``map_document.FORMAT``
#: rather than imported: :func:`_validate_map` deliberately stays terrain-blind
#: and does not reach ``map_document.py``, so this is a literal, not a re-export.
MAP_FORMAT = "fivee-sim-map"

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


def canonical_sha256(value: Any) -> str:
    """Stable SHA-256 of a JSON value, independent of mapping insertion order."""
    return sha256_of(canonical_json(value))


def sheet_sha256(entry: Mapping[str, Any]) -> str:
    """One combatant's static half, digested so a stale sheet cannot stay quiet.

    :data:`~fivee_sim.model.encounter.SHEET_KEYS` is a **bandwidth** claim — the
    keys a fight is believed not to move, and so the keys a later response need
    not repeat. This is what makes being wrong about one cost bandwidth rather
    than correctness: the digest is taken over the sheet **as serialised**, from
    the live payload, every time it is asked for. A declared-static field that
    ever moves — a new effect, a content pack, a defect — moves this with it, and
    a caller holding the old digest refetches instead of believing what it has.

    Canonical rather than ``json.dumps``, so a caller can recompute it from the
    payload it received and the published key set and get the same string; and
    **per combatant** rather than one digest over the roster, because a roster
    digest can always be derived from these and these can never be recovered
    from it. One creature whose sheet moved must not send a caller back for the
    other five.

    It lives here rather than beside the key sets because ``model/`` may not
    reach ``service/``: canonical hashing is this module's, and the
    classification is the model's. :func:`~fivee_sim.model.encounter.sheet_of`
    is the seam between them.
    """
    return canonical_sha256(sheet_of(entry))


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
        "temp_hp": creature.temp_hp,
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
        # Unconditional like ``Encounter._creature_state``'s own emission — see
        # the ruling there. Only entries above level 1 are named; level 1 is
        # what every condition without a level already reads as.
        "condition_levels": dict(sorted(
            (name, level) for name, level in creature.conditions.items() if level != 1
        )),
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


def replay_bundle_v2(
    *,
    name: str,
    engine_version: str,
    encounter_id: str,
    seed: int,
    movement_rule: str,
    mode: str,
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
    """Compose the additive, reconstructible replay-v2 envelope.

    Every checkpoint carries its state whole. That is what v3 changes and the
    only thing it changes, so this stays as the writer for a caller who names
    version 2 — a viewer somebody already has a copy of reads the version it
    was written for.
    """
    return _envelope(
        version=2,
        name=name,
        engine_version=engine_version,
        encounter_id=encounter_id,
        seed=seed,
        movement_rule=movement_rule,
        mode=mode,
        map_payload=map_payload,
        initial_creatures=initial_creatures,
        normalized_combatants=normalized_combatants,
        initial_state=initial_state,
        map_open_features=map_open_features,
        actions=actions,
        events=events,
        event_timestamps=event_timestamps,
        latest_state=latest_state,
        checkpoints=checkpoints,
        attempts=attempts,
        content_snapshot=content_snapshot,
    )


def replay_bundle_v3(
    *,
    name: str,
    engine_version: str,
    encounter_id: str,
    seed: int,
    movement_rule: str,
    mode: str,
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
    """The v2 envelope with its checkpoints thinned to a keyframe and a chain.

    It takes the same whole-state ``checkpoints`` v2 does and does the thinning
    here, because the shape of a v3 checkpoint is this format's business and
    :func:`validate_replay`, three hundred lines below, is what has to undo it.
    A caller that thinned them on the way in would put the two halves of one
    rule in two modules.

    ``state_hash`` keeps its v2 meaning exactly — the digest of the state the
    checkpoint stands for, not of the delta that expresses it — so a reader
    reconstructs and compares against the same number the version that wrote
    every state out in full would have recorded.
    """
    return _envelope(
        version=3,
        name=name,
        engine_version=engine_version,
        encounter_id=encounter_id,
        seed=seed,
        movement_rule=movement_rule,
        mode=mode,
        map_payload=map_payload,
        initial_creatures=initial_creatures,
        normalized_combatants=normalized_combatants,
        initial_state=initial_state,
        map_open_features=map_open_features,
        actions=actions,
        events=events,
        event_timestamps=event_timestamps,
        latest_state=latest_state,
        checkpoints=_chained_checkpoints(checkpoints),
        attempts=attempts,
        content_snapshot=content_snapshot,
    )


def _chained_checkpoints(
    checkpoints: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The first checkpoint whole, every later one as what moved since the last.

    Deltas run against the *previous checkpoint's state* rather than against the
    keyframe, so a reader applies them in order and holds one payload rather
    than a growing pile of patches to compose.
    """
    chained: list[dict[str, Any]] = []
    previous: Mapping[str, Any] | None = None
    for checkpoint in checkpoints:
        entry = dict(checkpoint)
        state = entry.get("state")
        if previous is not None and isinstance(state, Mapping):
            entry.pop("state")
            entry["state_delta"] = state_delta(
                previous, state, rosters=STATE_ROSTERS, entries=STATE_ENTRIES
            )
        if isinstance(state, Mapping):
            previous = state
        chained.append(entry)
    return chained


def _envelope(
    *,
    version: int,
    name: str,
    engine_version: str,
    encounter_id: str,
    seed: int,
    movement_rule: str,
    mode: str,
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
    """The block v2 and v3 share, stamped with the version that asked for it.

    ``version`` is passed rather than read off :data:`LATEST_FORMAT_VERSION`,
    which is what this composer used to do. That was correct only while there
    was one writer: bumping the constant would otherwise have had the v2 writer
    stamp its v2 checkpoints as v3.
    """
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
        "format_version": version,
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
            # Which kind of chapter this was. Required of the caller rather than
            # defaulted, because it is the one fact in this block a reader
            # cannot recover from anything else in the bundle: an interlude
            # rolls no initiative and holds no floor, so its states differ from
            # a fight's by an *absence*, and absence is exactly what a default
            # would let a caller record by accident.
            "mode": mode,
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


def _declared_mode(payload: Mapping[str, Any]) -> EncounterMode | None:
    """The kind of chapter a v2 bundle says it is, or ``None`` for an unreadable one.

    ``None`` covers two different documents and deliberately reads them the same
    way. One is a bundle frozen **before** interludes existed: it carries no
    ``mode``, every such fight was a fight, and refusing the key's absence would
    make every replay already on a user's disk unplayable at the version that
    added it. The other is a bundle whose ``mode`` is not a mode at all, which is
    diagnosed where it stands — and then falls back to a fight's *stricter*
    reading here, so a document that says nothing credible about itself is never
    granted the interlude's relaxation on top of the complaint it already earned.
    """
    encounter = payload.get("encounter")
    if not isinstance(encounter, Mapping):
        return None
    try:
        return EncounterMode(encounter["mode"])
    except (KeyError, TypeError, ValueError):
        return None


def _validate_state(
    value: Any, path: str, found: list[dict[str, str]], *, mode: EncounterMode | None
) -> None:
    if not isinstance(value, Mapping):
        found.append(_diagnostic(path, "must be an object"))
        return
    if not isinstance(value.get("round"), int) or isinstance(value.get("round"), bool):
        found.append(_diagnostic(f"{path}.round", "must be an integer"))
    # Nobody holds the floor between an interlude's beats, so its every state
    # payload reports ``turn`` as null. The rule is conditioned rather than
    # relaxed: a *fight* with no turn is still a broken bundle, and an interlude
    # naming somebody is claiming an initiative order that was never rolled.
    turn = value.get("turn")
    if mode is EncounterMode.EXPLORATION:
        if turn is not None:
            found.append(
                _diagnostic(f"{path}.turn", "must be null in an interlude")
            )
    elif not isinstance(turn, str):
        found.append(_diagnostic(f"{path}.turn", "must be a string"))
    _validate_combatants(value.get("combatants"), f"{path}.combatants", found)


def _validate_legend(value: Any, path: str, found: list[dict[str, str]]) -> dict[str, str]:
    """The legend as glyph -> kind, keeping only the entries that parse.

    A structurally bad legend is one finding, not a flood: an entry that fails
    is reported and dropped, exactly as :func:`_validate_map`'s callers treat a
    bad grid key or a bad feature independently of its siblings. The dict
    returned is used best-effort by the tile-glyph check below.
    """
    legend: dict[str, str] = {}
    if not isinstance(value, Mapping):
        found.append(_diagnostic(path, "must be an object"))
        return legend
    for glyph, kind in value.items():
        if not isinstance(glyph, str) or len(glyph) != 1:
            found.append(_diagnostic(path, f"glyph {glyph!r} must be a single character"))
            continue
        if not isinstance(kind, str) or not kind.strip():
            found.append(_diagnostic(path, f"glyph {glyph!r} must name a non-empty terrain kind"))
            continue
        legend[glyph] = kind
    return legend


def _validate_tiles(
    tiles: Any,
    path: str,
    grid: Mapping[str, Any] | None,
    legend: dict[str, str] | None,
    found: list[dict[str, str]],
) -> None:
    """Geometry and legend membership, terrain-blind: rows, width, glyphs.

    ``legend`` is ``None`` when the document's own legend was not a usable
    mapping at all, in which case the glyph check is skipped rather than
    reporting every character as unknown against an empty legend nobody wrote.
    """
    if not isinstance(tiles, list) or any(not isinstance(row, str) for row in tiles):
        found.append(_diagnostic(path, "must be an array of strings"))
        return
    height = grid.get("height") if isinstance(grid, Mapping) else None
    if isinstance(height, int) and not isinstance(height, bool) and len(tiles) != height:
        found.append(
            _diagnostic(path, f"has {len(tiles)} rows; the grid is {height} squares high")
        )
    width = grid.get("width") if isinstance(grid, Mapping) else None
    if isinstance(width, int) and not isinstance(width, bool):
        for y, row in enumerate(tiles):
            if len(row) != width:
                found.append(
                    _diagnostic(
                        f"{path}.{y}",
                        f"is {len(row)} characters; the grid is {width} squares wide",
                    )
                )
    if legend is not None:
        unknown: dict[str, tuple[int, int]] = {}
        for y, row in enumerate(tiles):
            for x, char in enumerate(row):
                if char not in legend and char not in unknown:
                    unknown[char] = (x, y)
        for char in sorted(unknown):
            x, y = unknown[char]
            found.append(
                _diagnostic(
                    path, f"row {y} column {x} uses {char!r}, which the legend does not define"
                )
            )


def _validate_features(
    features: Any,
    path: str,
    grid: Mapping[str, Any] | None,
    claimed: set[str],
    found: list[dict[str, str]],
) -> None:
    """``id``/``kind``/``at``, terrain-blind. ``claimed`` spans the document.

    Ids are unique across the whole document, not one level's features, which
    is why ``claimed`` is threaded through every call rather than reset per
    level: ``map_document.py``'s own ``_parse_features`` makes the same choice
    and for the same reason.
    """
    if not isinstance(features, list) or any(
        not isinstance(feature, Mapping) for feature in features
    ):
        found.append(_diagnostic(path, "must be an array of objects"))
        return
    width = grid.get("width") if isinstance(grid, Mapping) else None
    height = grid.get("height") if isinstance(grid, Mapping) else None
    grid_known = (
        isinstance(width, int) and not isinstance(width, bool)
        and isinstance(height, int) and not isinstance(height, bool)
    )
    for index, feature in enumerate(features):
        item_path = f"{path}.{index}"
        feature_id = feature.get("id")
        if not isinstance(feature_id, str) or not feature_id.strip():
            found.append(_diagnostic(f"{item_path}.id", "must be a non-empty string"))
        elif feature_id in claimed:
            found.append(
                _diagnostic(f"{item_path}.id", "must be unique across the document")
            )
        else:
            claimed.add(feature_id)
        kind = feature.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            found.append(_diagnostic(f"{item_path}.kind", "must be a non-empty string"))
        at = feature.get("at")
        if (
            not isinstance(at, (list, tuple))
            or len(at) != 2
            or any(isinstance(coord, bool) or not isinstance(coord, int) for coord in at)
        ):
            found.append(_diagnostic(f"{item_path}.at", "must be [x, y] square indices"))
        elif grid_known and not (0 <= at[0] < width and 0 <= at[1] < height):
            found.append(_diagnostic(f"{item_path}.at", "must be on the grid"))


def _validate_map(value: Any, found: list[dict[str, str]]) -> None:
    """The terrain-independent half of a map document's rules.

    This mirrors ``map_document.py``'s glyph and geometry checks without
    calling it: resolving a legend glyph to a terrain kind needs a
    ``TerrainTable``, which needs the bundle's own content pack rebuilt, and
    that is deliberately out of scope here — see the module docstring on
    ``validate_replay``. What is checked is exactly what crashes the renderer
    without ever asking what a terrain kind means: the format discriminator,
    the fixed 5-foot grid, legend shape, glyph-legend membership, row/column
    geometry, and feature identity and placement.
    """
    if value is None:
        return
    if not isinstance(value, Mapping):
        found.append(_diagnostic("map", "must be an object or null"))
        return
    if value.get("format") != MAP_FORMAT:
        found.append(_diagnostic("map.format", f"must be {MAP_FORMAT!r}"))
    version = value.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool):
        found.append(_diagnostic("map.format_version", "must be an integer"))
    name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        found.append(_diagnostic("map.name", "must be a non-empty string"))
    grid = value.get("grid")
    if not isinstance(grid, Mapping):
        found.append(_diagnostic("map.grid", "must be an object"))
        grid = None
    else:
        for key in ("width", "height", "cell_feet"):
            field = grid.get(key)
            if not isinstance(field, int) or isinstance(field, bool) or field <= 0:
                found.append(_diagnostic(f"map.grid.{key}", "must be a positive integer"))
        cell_feet = grid.get("cell_feet")
        if (
            isinstance(cell_feet, int)
            and not isinstance(cell_feet, bool)
            and cell_feet > 0
            and cell_feet != FEET_PER_SQUARE
        ):
            found.append(
                _diagnostic(
                    "map.grid.cell_feet",
                    f"must be {FEET_PER_SQUARE}; this format is defined on 5-foot squares",
                )
            )
    legend_raw = value.get("legend")
    legend = _validate_legend(legend_raw, "map.legend", found)
    legend_for_tiles = legend if isinstance(legend_raw, Mapping) else None
    claimed: set[str] = set()
    _validate_tiles(value.get("tiles"), "map.tiles", grid, legend_for_tiles, found)
    _validate_features(value.get("features", []), "map.features", grid, claimed, found)
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
            _validate_tiles(
                level.get("tiles"), f"{level_path}.tiles", grid, legend_for_tiles, found
            )
            _validate_features(
                level.get("features", []), f"{level_path}.features", grid, claimed, found
            )


def _checkpoint_state(
    checkpoint: Mapping[str, Any],
    index: int,
    version: Any,
    held: Mapping[str, Any] | None,
    found: list[dict[str, str]],
) -> dict[str, Any] | None:
    """The state a checkpoint stands for — carried whole, or rebuilt from the last.

    v1 and v2 write every checkpoint out in full, so validating one is a
    comparison. v3 writes a keyframe and then only what moved, so the state a
    checkpoint claims a ``state_hash`` over is not in the file and the validator
    has to build it. That is the point rather than a cost of the format: a
    validator that shrugged at a delta it could not apply would accept a bundle
    no viewer can play, which is the one thing this operation exists to rule out.

    A chain that has already broken says so once per link rather than reporting
    a mismatched hash for every checkpoint downstream of the real fault.
    """
    if version not in CHAINED_FORMAT_VERSIONS or index == 0:
        state = checkpoint.get("state")
        if not isinstance(state, Mapping):
            found.append(_diagnostic(f"checkpoints.{index}.state", "must be an object"))
            return None
        return dict(state)
    delta = checkpoint.get("state_delta")
    if not isinstance(delta, Mapping):
        found.append(
            _diagnostic(f"checkpoints.{index}.state_delta", "must be an object")
        )
        return None
    if held is None:
        found.append(
            _diagnostic(
                f"checkpoints.{index}.state_delta",
                "cannot be applied: the checkpoint before it did not reconstruct",
            )
        )
        return None
    return apply_state_delta(
        held, delta, rosters=STATE_ROSTERS, entries=STATE_ENTRIES
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
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in READABLE_FORMAT_VERSIONS
    ):
        allowed = " or ".join(str(item) for item in sorted(READABLE_FORMAT_VERSIONS))
        found.append(_diagnostic("format_version", f"must be {allowed}"))
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
    if version not in ENVELOPED_FORMAT_VERSIONS:
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
        # Graded against the model's own declaration rather than a set spelled
        # out here, for the reason the route table's enum is: this is a closed
        # set with an owner. Absence is not a finding — see :func:`_declared_mode`.
        if "mode" in encounter:
            try:
                EncounterMode(encounter["mode"])
            except (TypeError, ValueError):
                allowed = ", ".join(one.value for one in EncounterMode)
                found.append(
                    _diagnostic("encounter.mode", f"must be one of: {allowed}")
                )
    mode = _declared_mode(payload)
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
        _validate_state(initial.get("state"), "initial.state", found, mode=mode)
    _validate_state(payload.get("latest_state"), "latest_state", found, mode=mode)
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
        held: Mapping[str, Any] | None = None
        for index, checkpoint in enumerate(checkpoints):
            if not isinstance(checkpoint, Mapping):
                found.append(
                    _diagnostic(f"checkpoints.{index}", "must be an object")
                )
                held = None
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
            state = _checkpoint_state(checkpoint, index, version, held, found)
            held = state
            if state is None:
                continue
            _validate_state(state, f"checkpoints.{index}.state", found, mode=mode)
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
        if checkpoints and held is not None and held != payload.get("latest_state"):
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
        _validate_chapter_recovery(chapter, at, index, found)
        nested = chapter.get("replay")
        if not isinstance(nested, Mapping):
            found.append(_diagnostic(f"{at}.replay", "must be an object"))
            continue
        _validate_chapter_mode(chapter, nested, at, found)
        found.extend(
            _diagnostic(f"{at}.replay.{one['path']}", one["problem"])
            for one in validate_replay(nested)
        )


def _validate_chapter_recovery(
    chapter: Mapping[str, Any], at: str, index: int, found: list[dict[str, str]]
) -> None:
    """Grade the optional caller-stated boundary without inventing rest rules."""
    if "recovery" not in chapter:
        if "recovery_note" in chapter:
            found.append(
                _diagnostic(f"{at}.recovery_note", "requires recovery")
            )
        return
    if index == 0:
        found.append(
            _diagnostic(f"{at}.recovery", "must be absent from the first chapter")
        )
    recovery = chapter["recovery"]
    if not isinstance(recovery, Mapping):
        found.append(_diagnostic(f"{at}.recovery", "must be an object"))
    else:
        for name, delta in recovery.items():
            path = f"{at}.recovery.{name}"
            if not isinstance(name, str) or not name.strip():
                found.append(
                    _diagnostic(f"{at}.recovery", "combatant names must be non-empty strings")
                )
            if not isinstance(delta, Mapping):
                found.append(_diagnostic(path, "must be an object"))
    if "recovery_note" in chapter:
        note = chapter["recovery_note"]
        if not isinstance(note, str) or not note.strip():
            found.append(
                _diagnostic(f"{at}.recovery_note", "must be a non-empty string")
            )


def _validate_chapter_mode(
    chapter: Mapping[str, Any],
    nested: Mapping[str, Any],
    at: str,
    found: list[dict[str, str]],
) -> None:
    """Which kind of chapter the envelope says this was, held to the artifact.

    Two checks, because the field is a *copy*. Membership grades it against the
    model's own declaration; agreement grades it against the bundle it sits
    beside, which is where the copy came from. Without the second, an envelope
    could summarise a run as three fights while carrying an interlude, and a
    reader trusting the summary would draw an initiative order for a chapter
    that never rolled one.

    A chapter that says nothing is not a finding, for the reason
    :func:`_declared_mode` gives: an envelope composed before chapters carried
    the field is still a playable record of a run of fights.
    """
    if "mode" not in chapter:
        return
    declared = chapter["mode"]
    try:
        EncounterMode(declared)
    except (TypeError, ValueError):
        allowed = ", ".join(one.value for one in EncounterMode)
        found.append(_diagnostic(f"{at}.mode", f"must be one of: {allowed}"))
        return
    frozen = _declared_mode(nested)
    if frozen is not None and frozen != declared:
        found.append(
            _diagnostic(
                f"{at}.mode",
                f"says {declared!r} where its own replay says {frozen.value!r}",
            )
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
