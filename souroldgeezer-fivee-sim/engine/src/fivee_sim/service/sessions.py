"""Who owns the engine's live state, and for how long.

One :class:`EngineState` holds every fight in progress, every loaded map, and
the active content registry. It is passed to the service functions that need
it rather than reached for, which is the whole difference from the module-level
dictionaries this replaced: an adapter constructs one and hands it down, a test
constructs one and hands it down, and neither has to reach into another
module's globals to do it.

The durable half lives here too, because it is the same ownership question
asked of disk. A session tracks the journal head it last wrote, so its next
append can refuse to chain onto someone else's fight; a map session tracks the
bytes it last saw on disk for the same reason. Recovery is the inverse — a
journal read back into a session that is a *continuation* of what is on disk
rather than a second copy of it.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from random import Random
from typing import Any

from ..content import (
    ContentError,
    ContentRegistry,
    DataError,
    builtin_mode,
    builtin_registry,
    load_packs,
    registry_from_snapshot,
)
from ..kernel.grid import DiagonalRule, as_point
from ..map_document import MapDocument, to_grid
from ..map_document import serialize as serialize_map
from ..model.battlemap import BattleMap
from ..model.creature import Creature
from ..model.encounter import Encounter
from . import encounter_journal as journal_service
from . import maps as map_service
from . import specs
from .common import sha256_of
from .errors import RequestError

__all__ = [
    "Content",
    "EngineState",
    "MapSession",
    "Session",
    "active_content",
    "active_registry",
    "attempt_finished",
    "attempt_started",
    "cached_request",
    "capture_checkpoint",
    "initial_creatures",
    "journal_append",
    "map_session_for",
    "map_source_of",
    "new_encounter",
    "new_encounter_id",
    "new_map_id",
    "recover_session",
    "resolve_battle_map",
    "session_for",
    "utc_now",
]


@dataclass(slots=True)
class Session:
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
    #: The journal chain head this session last wrote. Every server on a host
    #: shares the encounter directory, so this is what distinguishes "my copy of
    #: the fight is current" from "someone else has advanced it" — without it a
    #: second process would append its own divergent turns to the same journal.
    journal_head: str = ""
    finalized: bool = False
    finalization_result: dict[str, Any] | None = None


@dataclass(slots=True)
class MapSession:
    """One loaded map. The document is frozen; edits replace it and bump the
    generation, which is how an encounter built from it can tell it moved on."""

    document: MapDocument
    generation: int = 1
    path: str | None = None
    #: The bytes this session last saw at :attr:`path` — set when it loaded the
    #: file or wrote it, and deliberately *not* updated by an edit, since an edit
    #: moves the document away from disk rather than moving disk. It is what lets
    #: a save guard itself without the caller supplying a hash.
    disk_sha256: str = ""


@dataclass(slots=True)
class Content:
    """The active registry, replaced wholesale rather than mutated."""

    registry: ContentRegistry
    generation: int = 1
    configured: tuple[str, ...] = ()
    #: Set when content named by the environment could not be loaded at start-up.
    startup_error: str = ""


@dataclass(slots=True)
class EngineState:
    """Every fight, map, and registry one engine process is holding.

    Passed to the service functions that touch it. An adapter owns exactly one
    and threads it down; nothing here is reachable any other way, which is what
    makes a second adapter — or a test — able to run the same tool bodies
    against state it constructed itself.
    """

    sessions: dict[str, Session] = field(default_factory=dict)
    maps: dict[str, MapSession] = field(default_factory=dict)
    content: Content | None = None
    next_id: int = 0
    next_map_id: int = 0


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# --- content ---------------------------------------------------------------
def active_content(state: EngineState) -> Content:
    """The active content, loaded from the environment on first use.

    A pack the environment names but that will not load must not take the server
    down with it: the process would fail its handshake and the user would get no
    tools at all, with the reason invisible. Instead the built-in slice loads, the
    failure goes to stderr, and ``content_status`` reports it — so the session works
    and the problem is still discoverable.
    """
    if state.content is None:
        try:
            state.content = Content(registry=load_packs(builtin=builtin_mode()))
        except ContentError as error:
            print(f"fivee-sim: falling back to bundled content: {error}", file=sys.stderr)
            state.content = Content(registry=builtin_registry(), startup_error=str(error))
    return state.content


def active_registry(state: EngineState) -> ContentRegistry:
    return active_content(state).registry


# --- identity --------------------------------------------------------------
def new_encounter_id(state: EngineState) -> str:
    while True:
        state.next_id += 1
        candidate = f"enc-{state.next_id}"
        try:
            exists = journal_service.journal_path(candidate).exists()
        except journal_service.JournalError:
            exists = False
        if candidate not in state.sessions and not exists:
            return candidate


def new_map_id(state: EngineState) -> str:
    state.next_map_id += 1
    return f"map-{state.next_map_id}"


def session_for(state: EngineState, encounter_id: str) -> Session:
    found = state.sessions.get(encounter_id)
    if found is None:
        found, _ = recover_session(state, encounter_id)
    return found


def map_session_for(state: EngineState, map_id: str) -> MapSession:
    found = state.maps.get(map_id)
    if found is None:
        known = ", ".join(sorted(state.maps)) or "none"
        raise RequestError(f"unknown map {map_id!r}; active: {known}")
    return found


# --- building a fight ------------------------------------------------------
def new_encounter(
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


def initial_creatures(encounter: Encounter) -> list[dict[str, Any]]:
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


# --- the map a fight is on -------------------------------------------------
def map_source_of(state: EngineState, session: Session) -> dict[str, Any] | None:
    """How the fight's map relates to the live map session, or ``None``.

    ``stale`` flips when the map has been edited or reloaded since the fight
    captured it — the fight keeps resolving on what it captured, and this is
    the divergence made visible, exactly as content generations work.
    """
    if session.map_id is None:
        return None
    live = state.maps.get(session.map_id)
    current = live.generation if live is not None else None
    return {
        "map_id": session.map_id,
        "generation": session.map_generation,
        "current_generation": current,
        "stale": current != session.map_generation,
    }


def resolve_battle_map(
    state: EngineState, map_spec: dict[str, Any] | None, map_id: str | None
) -> tuple[BattleMap | None, dict[str, Any] | None]:
    """The battle map a tool call names — inline spec or loaded session.

    A session-backed map also yields the ``map_source`` capture: which map,
    which generation, and the hash of the exact document the fight is on.
    The shape matches :func:`map_source_of`, so a caller reads ``stale`` off
    either tool's result — at capture time it is ``False`` by construction —
    plus ``sha256``, which only the capture can name.
    """
    if map_spec is not None and map_id is not None:
        raise RequestError("give 'map' (an inline spec) or 'map_id' (a loaded map), not both")
    if map_spec is not None:
        return specs.battle_map_from_spec(map_spec), None
    if map_id is not None:
        found = map_session_for(state, map_id)
        return to_grid(found.document), {
            "map_id": map_id,
            "generation": found.generation,
            "current_generation": found.generation,
            "stale": False,
            "sha256": sha256_of(serialize_map(found.document)),
        }
    return None, None


# --- the durable record ----------------------------------------------------
def capture_checkpoint(session: Session, timestamp: str) -> None:
    session.state_history.append(deepcopy(session.encounter.state()))
    session.checkpoint_event_counts.append(len(session.encounter.log))
    session.checkpoint_timestamps.append(timestamp)


def journal_append(
    state: EngineState,
    encounter_id: str,
    payload: Mapping[str, Any],
    session: Session | None = None,
) -> dict[str, Any]:
    """Append on this session's behalf, refusing to write from a stale copy.

    Passing the session opts into the ownership check: if another process has
    advanced this encounter, our in-memory copy is a different fight and writing
    from it would splice the two. Dropping the session forces the next call to
    recover the journal's version rather than continue from ours.
    """
    try:
        record = journal_service.append(
            encounter_id,
            payload,
            expected_head=None if session is None else session.journal_head,
        )
    except journal_service.StaleWriteError as error:
        state.sessions.pop(encounter_id, None)
        raise RequestError(str(error)) from error
    except journal_service.JournalError as error:
        raise RequestError(str(error)) from error
    if session is not None:
        session.journal_head = str(record["sha256"])
    return record


def cached_request(session: Session, request_id: str | None) -> dict[str, Any] | None:
    if request_id is None:
        return None
    cached = session.request_results.get(request_id)
    if cached is None:
        return None
    if cached["status"] == "refused":
        raise RequestError(str(cached["error"]))
    result = cached.get("result")
    if not isinstance(result, Mapping):
        raise RequestError(f"request {request_id!r} has no recorded result")
    return deepcopy(dict(result))


def attempt_started(
    state: EngineState,
    encounter_id: str,
    session: Session,
    operation: str,
    arguments: Mapping[str, Any],
    request_id: str | None,
) -> tuple[int, str]:
    timestamp = utc_now()
    index = len(session.attempts)
    journal_append(
        state,
        encounter_id,
        {
            "kind": "attempt",
            "timestamp": timestamp,
            "index": index,
            "operation": operation,
            "request_id": request_id,
            "arguments": deepcopy(dict(arguments)),
        },
        session,
    )
    return index, timestamp


def attempt_finished(
    state: EngineState,
    encounter_id: str,
    session: Session,
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
    timestamp = utc_now()
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
        journal_append(state, encounter_id, {"kind": "result", **audit}, session)
    except RequestError:
        # The caller cannot safely continue from state the durable record did
        # not acknowledge. Dropping the cache forces recovery from the valid
        # prefix on the next access.
        state.sessions.pop(encounter_id, None)
        raise
    session.attempts.append(audit)
    if request_id is not None:
        session.request_results[request_id] = audit


# --- recovery --------------------------------------------------------------
def recover_session(
    state: EngineState, encounter_id: str
) -> tuple[Session, dict[str, str] | None]:
    try:
        records, warning = journal_service.read(encounter_id, repair_partial=True)
    except journal_service.JournalError as error:
        known = ", ".join(sorted(state.sessions)) or "none"
        if "unknown encounter" in str(error):
            raise RequestError(
                f"unknown encounter {encounter_id!r}; active: {known}"
            ) from error
        raise RequestError(str(error)) from error
    if not records or records[0].get("kind") != "creation":
        raise RequestError(f"encounter journal {encounter_id!r} has no creation record")
    created = records[0]
    captured_content = created.get("content")
    if not isinstance(captured_content, Mapping):
        raise RequestError(f"encounter journal {encounter_id!r} has no content snapshot")
    try:
        registry = registry_from_snapshot(captured_content)
    except ContentError as error:
        raise RequestError(f"cannot recover {encounter_id!r}'s content: {error}") from error
    normalized = created.get("combatants")
    if not isinstance(normalized, list):
        raise RequestError(f"encounter journal {encounter_id!r} has no combatants")
    captured_map = created.get("map")
    battle_map: BattleMap | None = None
    if isinstance(captured_map, Mapping):
        try:
            document, _ = map_service.parse_payload(
                captured_map,
                source=f"journal:{encounter_id}",
                terrain=registry.terrain_effects,
            )
        except (ValueError, DataError) as error:
            raise RequestError(f"cannot recover {encounter_id!r}'s map: {error}") from error
        battle_map = to_grid(document)
    seed = int(created["seed"])
    rng = Random(seed)
    encounter = new_encounter(
        specs.combatants_from_specs([dict(entry) for entry in normalized], registry),
        rng,
        registry,
        movement_rule=specs.parse_movement_rule(str(created["movement_rule"])),
        battle_map=battle_map,
    )
    session = Session(
        encounter=encounter,
        rng=rng,
        seed=seed,
        content_generation=int(created.get("content_generation", 0)),
        initial_creatures=initial_creatures(encounter),
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
    capture_checkpoint(session, created_at)

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
            encounter.act(specs.action_from_journal(record["arguments"]), rng)
            timestamp = str(record["timestamp"])
            session.event_timestamps.extend([timestamp] * (len(encounter.log) - before))
            capture_checkpoint(session, timestamp)
        elif status == "success" and operation == "encounter_advance":
            before = len(encounter.log)
            encounter.advance(rng)
            timestamp = str(record["timestamp"])
            session.event_timestamps.extend([timestamp] * (len(encounter.log) - before))
            capture_checkpoint(session, timestamp)
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
    # Recovery adopts the journal's head, not a fresh one: this session is now
    # a continuation of whatever is on disk, and its next append must chain onto
    # exactly the record it just read.
    session.journal_head = str(records[-1]["sha256"]) if records else ""
    state.sessions[encounter_id] = session
    if encounter_id.startswith("enc-") and encounter_id[4:].isdigit():
        state.next_id = max(state.next_id, int(encounter_id[4:]))
    return session, warning
