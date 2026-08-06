"""Who owns the engine's live state, and for how long.

One :class:`EngineState` holds every fight in progress and the active content
registry. It is passed to the service functions that need it rather than
reached for, which is the whole difference from the module-level dictionaries
this replaced: an adapter constructs one and hands it down, a test constructs
one and hands it down, and neither has to reach into another module's globals
to do it.

Maps are not in it, and that is the point. A map is a file addressed by id;
what a fight keeps is the hash of the document it started on, so "has the map
moved since?" is answered against the file every process can see rather than
against a private copy each process believed was current.

The durable half lives here too, because it is the same ownership question
asked of disk. A session tracks the journal head it last wrote, so its next
append can refuse to chain onto someone else's fight. Recovery is the inverse —
a journal read back into a session that is a *continuation* of what is on disk
rather than a second copy of it.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
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
from ..map_document import MapDocument
from ..map_document import serialize as serialize_map
from ..model.creature import Creature
from ..model.encounter import Encounter, EncounterError, EncounterMode
from . import encounter_journal as journal_service
from . import maps as map_service
from . import specs
from .common import sha256_of
from .errors import NotFoundError, RequestError

__all__ = [
    "DOCUMENT_MARKER",
    "Content",
    "EngineState",
    "ResolvedMap",
    "Session",
    "active_content",
    "active_registry",
    "attempt_finished",
    "attempt_started",
    "cached_request",
    "capture_checkpoint",
    "initial_creatures",
    "journal_append",
    "map_source_of",
    "maps_dir_of",
    "new_encounter",
    "new_encounter_id",
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
    #: The saved map this fight was built from, if any, and the hash of the
    #: exact document it captured — the same staleness idiom as content: an edit
    #: to the file never reaches into a fight, so the divergence is reported
    #: instead. The hash is the whole handle, because the file is the truth:
    #: there is no session in between to carry a generation counter.
    map_id: str | None = None
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
class Content:
    """The active registry, replaced wholesale rather than mutated."""

    registry: ContentRegistry
    generation: int = 1
    configured: tuple[str, ...] = ()
    #: Set when content named by the environment could not be loaded at start-up.
    startup_error: str = ""


@dataclass(slots=True)
class EngineState:
    """Every fight and registry one engine process is holding.

    Passed to the service functions that touch it. An adapter owns exactly one
    and threads it down; nothing here is reachable any other way, which is what
    makes a second adapter — or a test — able to run the same tool bodies
    against state it constructed itself.

    Maps are conspicuously absent, and their absence is the design: a map is a
    *file*, addressed by the id its filename gives it. There used to be a
    dictionary of loaded map sessions here, which meant two servers on one host
    each held a private copy of the same map and each thought its own was
    current. :attr:`maps_dir` is the one thing left — which directory this
    adapter's ids resolve in — and ``None`` means the configured root.
    """

    sessions: dict[str, Session] = field(default_factory=dict)
    content: Content | None = None
    next_id: int = 0
    maps_dir: Path | None = None


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
def _seed_from_disk(state: EngineState) -> None:
    """Start a fresh engine past whatever the directory already holds.

    Only a starting point, never a guarantee — another process can take the very
    next id before this one asks for it, which is what ``claim`` is for. Without
    it a new engine in a busy directory would walk from one, losing a claim per
    encounter already there.
    """
    highest = 0
    for path in journal_service.list_journals():
        suffix = path.stem[len("enc-"):]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    state.next_id = highest


def new_encounter_id(state: EngineState) -> str:
    """Take the next free id, and hold it against every other process.

    The claim is the journal file itself (see ``encounter_journal.claim``), so
    the id is spent the moment it is handed out rather than when the creation
    record lands — ``create`` does real work in between, and that gap used to be
    wide enough for a second engine to be handed the same name.
    """
    if state.next_id == 0:
        _seed_from_disk(state)
    while True:
        state.next_id += 1
        candidate = f"enc-{state.next_id}"
        if candidate in state.sessions:
            continue
        if journal_service.claim(candidate):
            return candidate


def session_for(state: EngineState, encounter_id: str) -> Session:
    found = state.sessions.get(encounter_id)
    if found is None:
        found, _ = recover_session(state, encounter_id)
    return found


def maps_dir_of(state: EngineState) -> Path:
    """Where this adapter's map ids resolve: its own directory, or the root."""
    return state.maps_dir if state.maps_dir is not None else map_service.maps_root()


# --- building a fight ------------------------------------------------------
def new_encounter(
    combatants: list[Creature],
    rng: Random,
    registry: ContentRegistry,
    *,
    movement_rule: DiagonalRule = DiagonalRule.FIVE_FIVE_FIVE,
    map_document: MapDocument | None = None,
    mode: EncounterMode = EncounterMode.COMBAT,
) -> Encounter:
    """Build an encounter bound to ``registry``'s tables, captured by value."""
    return Encounter(
        combatants,
        rng,
        spellbook=registry.spells,
        items=registry.items,
        condition_effects=registry.condition_effects,
        movement_rule=movement_rule,
        map_document=map_document,
        terrain_effects=registry.terrain_effects,
        mode=mode,
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
def current_map_sha256(state: EngineState, map_id: str) -> str | None:
    """The canonical hash of the saved map ``map_id`` names, or ``None``.

    ``None`` covers every way the file can fail to answer — deleted, renamed,
    no longer parsing — because from a fight's point of view they are the same
    fact: what it captured is no longer what is on disk.
    """
    try:
        path = map_service.resolve_id(map_id, maps_dir_of(state))
    except RequestError:
        return None
    return map_service.current_sha256(path, terrain=active_registry(state).terrain_effects)


def map_source_of(state: EngineState, session: Session) -> dict[str, Any] | None:
    """How the fight's map relates to the file it came from, or ``None``.

    ``stale`` flips when the file has changed since the fight captured it — the
    fight keeps resolving on what it captured, and this is the divergence made
    visible, exactly as content generations work. The comparison is against the
    file rather than against a loaded copy, because the file is the only thing
    two servers on one host both see.
    """
    if session.map_id is None:
        return None
    current = current_map_sha256(state, session.map_id)
    return {
        "map_id": session.map_id,
        "sha256": session.map_sha256,
        "current_sha256": current,
        "stale": current != session.map_sha256,
    }


#: What an inline map calls itself when it is a **document** rather than a
#: battle-map spec. The document format declares itself in ``format``; a spec
#: has no such key — ``specs.MAP_KEYS`` is closed and does not contain it — so
#: the inline value is self-identifying and the dispatch below reads it rather
#: than asking the caller to declare which shape they sent.
#:
#: Presence, never the value. An object claiming a format we do not speak is
#: judged by the document parser, which answers *must be "fivee-sim-map"*;
#: matching on the value instead would send it back to the spec parser, whose
#: only available complaint is that ``format`` is not a spec key — the
#: unhelpful refusal that hid this whole branch in the first place.
DOCUMENT_MARKER = "format"


@dataclass(frozen=True, slots=True)
class ResolvedMap:
    """The map a tool call named, and where it came from.

    It was a triple of optionals, then a pair, and it is one map now. A spec
    used to produce a battle map and nothing else, so a caller holding one had
    to render a document back out of it to write the fight down; then every
    producer made a :class:`~fivee_sim.map_document.MapDocument` and a bridge
    made the grid beside it, and the two had to travel together. A fight is
    handed the document itself, so there is nothing left for a second field to
    hold.

    ``source`` is the exception and stays optional: it answers *has the file
    changed since the fight started?*, and only a **saved** map has a file to
    have changed.
    """

    document: MapDocument
    source: dict[str, Any] | None = None


def resolve_battle_map(
    state: EngineState, map_spec: dict[str, Any] | None, map_id: str | None
) -> ResolvedMap | None:
    """The map a tool call names — inline map or saved map file — or none.

    An inline ``map`` is either a battle-map **spec** — the ``width``/``height``/
    ``rows``/``legend`` form a person or a model writes by hand — or a whole
    ``fivee-sim-map`` **document**, which is what the browser editor's Play
    button posts when its buffer has never been saved and so has no id to name.
    :data:`DOCUMENT_MARKER` tells them apart, and both end in the same place a
    saved map does: a document, which is the map a fight resolves on. A spec
    takes the shorter road —
    :func:`~fivee_sim.service.specs.document_from_spec` builds the document
    rather than parsing one, so a spec's refusals stay the spec's own — but it
    arrives at the same artifact. Inline is not a laxer door onto the same map:
    a malformed buffer raises the same
    :class:`~fivee_sim.map_document.MapError`, carrying every diagnostic, that a
    malformed file does.

    A saved map also yields the ``map_source`` capture (which map, and the hash
    of the exact document the fight is on), so a caller that must snapshot it by
    value does not read the file a second time and risk snapshotting a different
    version than it resolved. The capture's shape matches :func:`map_source_of`,
    so a caller reads ``stale`` off either result — at capture time it is
    ``False`` by construction.

    An inline map deliberately gets **no** ``map_source``. That capture answers
    one question — *has the file changed since the fight started?* — and an
    inline map has no file to have changed: the id would resolve to nothing and
    ``stale`` could never become true, so a capture here would be a fabricated
    provenance rather than a missing one. What a fight needs instead is the map
    itself, and it gets it: :mod:`~fivee_sim.service.encounters` captures the
    document *whole* in the creation journal and a replay is on the map the
    table played on.
    """
    if map_spec is not None and map_id is not None:
        raise RequestError(
            "give 'map' (an inline spec or map document) or 'map_id' (a saved map), "
            "not both"
        )
    terrain = active_registry(state).terrain_effects
    if map_spec is not None:
        if DOCUMENT_MARKER in map_spec:
            document, _warnings = map_service.parse_payload(
                map_spec, source="inline map", terrain=terrain
            )
        else:
            document = specs.document_from_spec(map_spec, terrain)
        return ResolvedMap(document=document)
    if map_id is not None:
        document, _path = map_service.load_by_id(
            map_id, maps_dir_of(state), terrain=terrain
        )
        sha256 = sha256_of(serialize_map(document))
        return ResolvedMap(
            document=document,
            source={
                "map_id": map_id,
                "sha256": sha256,
                "current_sha256": sha256,
                "stale": False,
            },
        )
    return None


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
            raise NotFoundError(
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
    map_document: MapDocument | None = None
    if isinstance(captured_map, Mapping):
        try:
            document, _ = map_service.parse_payload(
                captured_map,
                source=f"journal:{encounter_id}",
                terrain=registry.terrain_effects,
            )
        except (ValueError, DataError) as error:
            raise RequestError(f"cannot recover {encounter_id!r}'s map: {error}") from error
        map_document = document
    seed = int(created["seed"])
    rng = Random(seed)
    # Which kind of chapter this was, defaulted for every journal written before
    # there was a second kind. It reaches both calls below and neither is
    # optional: the roster rules differ by mode, so a solo interlude recovers
    # only if the count knows what it is counting for; and every act replayed
    # further down names its actor, which a chapter recovered as a fight would
    # refuse outright.
    mode = specs.parse_mode(str(created.get("mode", EncounterMode.COMBAT.value)))
    # Translated exactly as ``encounters.create`` translates the same call, and
    # for a sharper reason: this is a re-run of the whole of
    # ``Encounter.__init__`` — the roster rules and every map rule
    # ``_adopt_map`` asks — over a record this process did not necessarily
    # write. A journal repaired by hand, or written by a build whose rules have
    # since moved, refuses here; bare, that refusal is not a ``RequestError``
    # and reaches the adapter's last resort as a 500. The three refusals above
    # already name the encounter, so this one does too.
    try:
        encounter = new_encounter(
            specs.combatants_from_specs(
                [dict(entry) for entry in normalized], registry, mode=mode
            ),
            rng,
            registry,
            movement_rule=specs.parse_movement_rule(str(created["movement_rule"])),
            map_document=map_document,
            mode=mode,
        )
    except EncounterError as error:
        raise RequestError(
            f"cannot recover {encounter_id!r}'s fight: {error}"
        ) from error
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
            # The actor is an *input* to the act, exactly as a supplied d20 face
            # is: an interlude has no initiative to re-derive it from, so a
            # replay that dropped it would be refused rather than resolving the
            # wrong creature. ``None`` for a fight, and for every act recorded
            # before this key existed — which is the same value they ran with.
            acted = record["arguments"]
            actor = acted.get("actor")
            encounter.act(
                specs.action_from_journal(acted),
                rng,
                actor=str(actor) if actor is not None else None,
            )
            timestamp = str(record["timestamp"])
            session.event_timestamps.extend([timestamp] * (len(encounter.log) - before))
            capture_checkpoint(session, timestamp)
        elif status == "success" and operation == "encounter_condition":
            # A ruling changes the fight, so recovery has to replay it like an
            # action. It consumes no randomness and emits into the log, so the
            # timestamps and checkpoint follow the same shape as the two below.
            before = len(encounter.log)
            arguments = record.get("arguments", {})
            encounter.set_condition(
                str(arguments["target"]),
                str(arguments["condition"]),
                applied=bool(arguments.get("applied", True)),
                levels=int(arguments.get("levels", 1)),
            )
            timestamp = str(record["timestamp"])
            session.event_timestamps.extend([timestamp] * (len(encounter.log) - before))
            capture_checkpoint(session, timestamp)
        elif status == "success" and operation == "encounter_advance":
            before = len(encounter.log)
            encounter.advance(
                rng, tuple(int(f) for f in record.get("arguments", {}).get("natural") or ())
            )
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
