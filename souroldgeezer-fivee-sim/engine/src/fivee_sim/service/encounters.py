"""The fight's own workflow: start it, read it, act in it, end it.

Every mutating step here is durable before it is answered. An attempt record
goes to the journal, the encounter is stepped, and a result record follows —
so a process that dies mid-turn leaves a journal that recovers to the last
acknowledged state rather than to a guess. ``request_id`` closes the other
half: a retry of a call that already landed returns the recorded result instead
of taking the turn twice.

Creation captures by value on purpose. The content registry and the map
document a fight starts on are snapshotted into the journal, so the fight
finishes under what it began with however the live registry or map moves
afterwards — and ``map_source`` reports that divergence rather than hiding it.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from copy import deepcopy
from pathlib import Path
from random import Random
from typing import Any

from .. import __version__
from ..kernel.conditions import UnknownCondition
from ..kernel.dice import DiceError
from ..kernel.grid import MovementMode
from ..map_document import as_payload
from ..model.encounter import (
    EVENT_LISTS,
    Action,
    ActionKind,
    EncounterError,
    EncounterMode,
)
from ..paths import source_id
from . import blobs, content_ops, map_ops, primitives, sessions, specs, views
from . import encounter_journal as journal_service
from . import replay as replay_service
from .errors import NotFoundError, RequestError
from .sessions import EngineState, Session

__all__ = [
    "act",
    "advance",
    "brief_for",
    "create",
    "event_log",
    "finalize",
    "list_encounters",
    "note",
    "prune",
    "replay_path",
    "require_seat",
    "resume",
    "state_of",
]


def require_seat(names: Collection[str], viewer: str) -> None:
    """Refuse a name this cast does not hold, in the sentence the whole surface uses.

    One sentence with one owner, because two callers ask this question about two
    different things. :func:`_briefed` asks it of a *snapshot*, which is every
    seat there is by the time a fight can be read; :func:`create` asks it of the
    roster a fight is being *started* from, where the refusal has to land before
    the encounter is created, journaled and given an id. A client that learns the
    refusal from one operation has learnt it from all of them — and
    :meth:`~fivee_sim.model.encounter.Encounter._seats_of` raises the same
    sentence from the model, which this translates rather than restates.

    ``NotFoundError`` rather than a plain refusal, so every door answers ``404``:
    a seat is a thing that is or is not in this fight, and ``GET .../brief`` and
    ``POST .../actions`` disagreeing about its status for one identical sentence
    would be a surface with two vocabularies.
    """
    if viewer not in names:
        raise NotFoundError(f"no combatant named {viewer!r} in this encounter")


def _briefed(
    session: Session, result: dict[str, Any], viewer: str | None
) -> dict[str, Any]:
    """One operation's answer, with the fight in it narrowed to a seat.

    :meth:`~fivee_sim.model.encounter.Encounter.brief` was this projection's only
    door for a release, and every operation that *changed* a fight answered
    whichever seat posted it with the GM's whole snapshot. A player's client
    could only hide what it had already been given, which is the failure the
    projection exists to avoid — inert against devtools, a proxy, or an
    extension. So the four operations that answer with a ``state`` pass it
    through here.

    ``None`` is *no chair asked*, and returns the result unchanged. That is the
    whole of the additive promise: the CLI and the skills name no seat and get
    back, byte for byte, the answer they always got.

    **The projected ``state`` is the brief's shape, not a redacted snapshot.**
    One projection, one shape: the brief is what the model can actually compute —
    total cover is a relationship between two creatures and a map, and no
    snapshot carries it — so a flat redacted ``state`` would be a second
    projection, with a second classification to keep in step and a weaker filter
    than this one. A caller that wants the flat shape omits ``as`` and is
    answered exactly as before.

    **The result is rebuilt, never edited.** The dictionary handed in is the one
    the journal recorded and the one an idempotent retry replays, so projecting
    it in place would rewrite a fight's own audit record into one player's brief
    and answer the GM's next retry with it.

    **``state`` and the events are narrowed together**, against one cast, so that
    a creature the ``state`` omits is not named by an event beside it. What is
    *not* narrowed is everything else in the answer: ``encounter_id``, ``seed``,
    a content warning, a recovery warning. None of them is anybody's sheet.
    """
    if viewer is None:
        return result
    snapshot: dict[str, Any] = result["state"]
    require_seat([str(one["name"]) for one in snapshot["combatants"]], viewer)
    encounter = session.encounter
    briefed: dict[str, Any] = {
        **result, "state": encounter.brief_of(snapshot, viewer)
    }
    for key in EVENT_LISTS:
        found = result.get(key)
        if isinstance(found, list):
            briefed[key] = encounter.brief_events(found, snapshot, viewer)
    return briefed


def _answered(
    session: Session,
    result: dict[str, Any],
    viewer: str | None,
    view: str,
) -> dict[str, Any]:
    """Every write's answer, and the only place the two projections compose.

    Seat first, view second — one spelling of it, so there is no per-operation
    order to keep in step and no fifth leak of the kind CLAUDE.md lists. The
    argument for that order is in :mod:`fivee_sim.service.views`; what matters
    here is that no caller can get it the other way round without editing this
    line.
    """
    return views.viewed(session, _briefed(session, result, viewer), viewer, view)


def _rebaselined(
    session: Session, payload: dict[str, Any], viewer: str | None
) -> dict[str, Any]:
    """Record a *read* as this seat's baseline, and return it untouched.

    A read is a payload the caller now holds, so it is a baseline by the same
    definition a write's answer is. Without this, a caller that refetched after
    a digest mismatch would be repaired while the server went on diffing against
    the payload it sent before the refetch — and a value that moved away and back
    would then be missing from every later delta, permanently, because the two
    ends agreed on it and neither was right.

    Reads never *serve* a delta: ``encounter.state`` is the full authority this
    phase leaves alone, and ``encounter.brief`` is the seat's whole picture.
    """
    session.last_payload[viewer] = deepcopy(payload)
    return payload


def replay_path(encounter_id: str) -> Path:
    """Where :func:`finalize` freezes this fight's replay bundle.

    One declaration because there are now two readers. ``finalize`` writes it,
    and ``service/adventures.py`` reads it back as a chapter of the run's own
    replay — a second spelling of this name in that module would be one more
    pair of declarations that must agree and nothing holding them together.

    It sits beside the journal rather than under the replays directory on
    purpose: this file is the *record* of a finished fight, addressed by
    encounter id, not a shareable export somebody chose a name for. Literally
    beside it now — the fight's own directory holds both — so the id lives in
    the directory name and the file is just ``replay.json``.
    """
    return journal_service.encounter_dir(encounter_id) / "replay.json"


def creation_response(
    state: EngineState, encounter_id: str, session: Session
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "encounter_id": encounter_id,
        "seed": session.seed,
        "content_generation": session.content_generation,
        "state": session.encounter.state(),
        "log": [event.as_dict() for event in session.encounter.log],
    }
    map_source = sessions.map_source_of(state, session)
    if map_source is not None:
        result["map_source"] = map_source
    return result


def creation_request(
    state: EngineState, request_id: str
) -> tuple[str, Session] | None:
    for path in journal_service.list_journals():
        encounter_id = path.parent.name
        try:
            summary = journal_service.head_and_tail(encounter_id)
        except journal_service.JournalError:
            continue
        if summary is None or summary.first.get("request_id") != request_id:
            continue
        # Only now, and only for the one that matched: recovering a session
        # replays the fight, which is what the scan was buying for every
        # journal on the disk to read one field off each creation record.
        return encounter_id, sessions.session_for(state, encounter_id)
    return None


def create(
    state: EngineState,
    combatants: list[dict[str, Any]],
    seed: int | None = None,
    movement_rule: str = "5-5-5",
    map_spec: dict[str, Any] | None = None,
    map_id: str | None = None,
    request_id: str | None = None,
    viewer: str | None = None,
    *,
    mode: str = EncounterMode.COMBAT.value,
    view: str | None = None,
) -> dict[str, Any]:
    """Start a chapter, and answer either the GM or one seat in it.

    ``viewer`` names a combatant in ``combatants``, and narrows the ``state``
    this returns to what that chair may hold. Omitted, nothing changes.

    ``mode`` says which kind of chapter it is — a fight by default, so every
    caller written before interludes existed starts exactly what it always
    started. Keyword-only because it is the first argument added past the
    positional chain above, and one more positional slot on a call this long is
    how the wrong value ends up in the right place.

    ``view`` defaults to ``full`` here, and there is no choice about it: this is
    the operation that *establishes* what a later delta is against. A caller may
    still ask for ``live`` and get the sheets as digests, which is only useful
    when it already holds them from an earlier fight.

    It is parsed before anything else runs, so a mistyped view is refused instead
    of starting a fight and then failing to describe it.
    """
    chosen = views.parse_view(view, views.FULL)
    if request_id is not None:
        existing = creation_request(state, request_id)
        if existing is not None:
            return _answered(
                existing[1], creation_response(state, *existing), viewer, views.FULL
            )
    chapter = specs.parse_mode(mode)
    used = specs.checked_seed(seed)
    rng = Random(used)
    content = sessions.active_content(state)
    resolved = sessions.resolve_battle_map(state, map_spec, map_id)
    try:
        built_combatants = specs.combatants_from_specs(
            combatants, content.registry, mode=chapter
        )
        encounter = sessions.new_encounter(
            built_combatants, rng, content.registry,
            movement_rule=specs.parse_movement_rule(movement_rule),
            map_document=resolved.document if resolved is not None else None,
            mode=chapter,
        )
    except EncounterError as error:
        raise RequestError(str(error)) from error
    # Before anything durable happens, and that is the whole reason it is here
    # rather than left to the projection at the end. ``briefed`` would refuse an
    # unknown seat too — but only after this fight had been created, journaled
    # and given an id, so a caller who mistyped their own name would get a 404
    # with an orphan encounter standing behind it. The roster is the authority
    # on who is in this fight, and it is already built.
    if viewer is not None:
        require_seat({one.name for one in built_combatants}, viewer)
    encounter_id = sessions.new_encounter_id(state)
    session = Session(
        encounter=encounter, rng=rng, seed=used,
        content_generation=content.generation,
    )
    session.initial_creatures = sessions.initial_creatures(encounter)
    session.initial_state = deepcopy(encounter.state())
    session.normalized_combatants = [
        replay_service.normalized_combatant_payload(creature)
        for creature in built_combatants
    ]
    session.content_snapshot = content_ops.content_snapshot(content.registry)
    created_at = sessions.utc_now()
    session.event_timestamps = [created_at] * len(encounter.log)
    sessions.capture_checkpoint(session, created_at)
    if encounter.map_state is not None:
        session.initial_open_features = sorted(encounter.map_state.open_features)
    if resolved is not None and resolved.source is not None:
        session.map_id = str(resolved.source["map_id"])
        session.map_sha256 = str(resolved.source["sha256"])
        # The payload, not a reference to the file: replay_export must see the
        # document as it stood when the fight started, whatever is written to
        # the map afterwards.
        session.map_payload = as_payload(resolved.document)
    elif resolved is not None:
        # An inline map has no id and no file, so nothing could fetch it again
        # and it travels by value or not at all — which is also why it gets no
        # ``map_source``. Whole, and as a document either way: a spec builds one
        # like every other producer, so there is nothing left here to render back
        # out of a grid and nothing left for that rendering to lose.
        session.inline_map_payload = as_payload(resolved.document)
    state.sessions[encounter_id] = session
    captured_map = session.map_payload or session.inline_map_payload
    # Published before the record that names them, and in that order for the
    # obvious reason: a journal naming a blob nobody wrote is a fight that
    # cannot recover, while a blob nobody names is a file, retained like every
    # other. The failure directions are not symmetric, so neither is the order.
    try:
        content_ref = blobs.put(session.content_snapshot)
        map_ref = blobs.put(captured_map) if captured_map is not None else None
    except blobs.BlobError as error:
        state.sessions.pop(encounter_id, None)
        raise RequestError(f"cannot store {encounter_id!r}'s payloads: {error}") from error
    try:
        sessions.journal_append(
            state,
            encounter_id,
            {
                "kind": "creation",
                # Which format the whole journal is written in, stamped once
                # and read back by ``recover_session`` before anything else in
                # the file is believed. See ``sessions.JOURNAL_VERSION``.
                "journal_version": sessions.JOURNAL_VERSION,
                "timestamp": created_at,
                "request_id": request_id,
                "encounter_id": encounter_id,
                "engine_version": __version__,
                # Which *build* wrote this journal, beside which release. The
                # release number cannot tell two checkouts of one release
                # apart, and that is exactly the case a journal outlives: the
                # documented ``FIVEE_SIM_RELOAD`` workflow replaces the engine
                # under a live fight, and a fight that then will not replay has
                # to be able to say so. Written once, in the creation record,
                # because this names the build the whole journal was recorded
                # under; ``""`` when the launcher was watching nothing, which
                # is an ordinary run and not an error.
                #
                # Nothing compares it. A fight outliving a release is normal,
                # so refusing on a mismatch would break cross-version recovery
                # for the sake of a diagnostic string.
                "source_id": source_id(),
                "seed": used,
                # Which kind of chapter this is, written once and durably: this
                # is the only place the mode survives a process, and recovery
                # reads it back to rebuild the same kind of encounter. Taken
                # from the encounter rather than from the argument, so what is
                # recorded is what was built.
                "mode": encounter.mode.value,
                "movement_rule": encounter.movement_rule.value,
                "content_generation": content.generation,
                # Named, not carried. Both used to ride here by value, and the
                # content was the larger of the two by an order of magnitude
                # *and* byte-identical in every journal on the machine — 244 KB
                # of disk holding 11 KB of information once. A blob is
                # addressed by its own digest, so the sharing needs no index
                # and the reference needs no version: what this name resolves
                # to cannot change. See ``service/blobs.py``.
                "content_ref": content_ref,
                "combatants": session.normalized_combatants,
                "map_ref": map_ref,
                # Not part of the blob, and deliberately: ``map_kind`` and
                # ``map_source`` are what ``adventures._creation_record`` reads
                # to decide whether a chapter's ground carries into the next
                # one, and neither is the document.
                "map_kind": (
                    "loaded" if session.map_payload is not None
                    else "inline" if session.inline_map_payload is not None
                    else "none"
                ),
                "map_source": resolved.source if resolved is not None else None,
                # Not the initial state. It used to ride here and nothing ever
                # read it back: ``recover_session`` recomputes it from the
                # encounter it has just rebuilt out of ``combatants``, ``seed``
                # and ``map``, which are the inputs it is derived from. The
                # in-memory ``Session.initial_state`` stays — ``map_ops`` reads
                # it for a v2 replay bundle — but it is a derivation, not a
                # record, and only one of those belongs in a journal.
                "map_open_features": session.initial_open_features,
            },
            session,
        )
    except RequestError:
        state.sessions.pop(encounter_id, None)
        raise
    result = creation_response(state, encounter_id, session)
    if content.startup_error:
        result["content_warning"] = (
            "configured content failed to load; this fight uses the bundled slice "
            "only. See content_status."
        )
    return _answered(session, result, viewer, chosen)


def brief_for(state: EngineState, encounter_id: str, as_name: str) -> dict[str, Any]:
    """One combatant's own view of the fight — see :meth:`Encounter.brief`.

    The model's refusal is translated rather than restated, and to the same
    ``404`` :func:`require_seat` raises: an unknown seat is an unknown seat
    whether it was named on a read or on a write.
    """
    session = sessions.session_for(state, encounter_id)
    try:
        brief = session.encounter.brief(as_name)
    except EncounterError as error:
        raise NotFoundError(str(error)) from error
    return _rebaselined(session, brief, as_name)


def _snapshot_of(state: EngineState, session: Session) -> dict[str, Any]:
    snapshot = session.encounter.state()
    snapshot["map_source"] = sessions.map_source_of(state, session)
    return snapshot


def state_of(state: EngineState, encounter_id: str) -> dict[str, Any]:
    session = sessions.session_for(state, encounter_id)
    return _rebaselined(session, _snapshot_of(state, session), None)


def note(
    state: EngineState,
    encounter_id: str,
    text: str,
    category: str = "note",
    request_id: str | None = None,
    speaker: str | None = None,
) -> dict[str, Any]:
    """Write a line of the record, optionally in somebody's voice.

    ``speaker`` names a combatant in this encounter, so a line can be drawn at
    that creature's token rather than floating over the map. It is checked
    against the roster before anything durable happens, for the reason
    :func:`act` checks its ``actor`` there: journaling an attempt for a creature
    this fight does not hold writes a beat nobody took into the record a replay
    is built from. The refusal is :func:`require_seat`'s one sentence, so an
    unknown name answers the same ``404`` from a note as from a brief.

    Nothing new is journaled. A note is already an audited attempt, so the
    speaker rides in that attempt's arguments and reaches the v2 bundle with it.
    """
    if speaker is not None:
        require_seat(sessions.session_for(state, encounter_id).encounter.creatures, speaker)

    def execute() -> dict[str, Any]:
        written = text.strip()
        label = category.strip()
        if not written:
            raise RequestError("note text must not be blank")
        if len(written) > MAX_NOTE_TEXT:
            raise RequestError(f"note text must be at most {MAX_NOTE_TEXT} characters")
        if not label:
            raise RequestError("note category must not be blank")
        return {
            "encounter_id": encounter_id,
            "text": written,
            "category": label,
            "speaker": speaker,
            "timestamp": sessions.utc_now(),
        }

    return primitives.audited_primitive(
        state,
        encounter_id=encounter_id,
        request_id=request_id,
        operation="encounter_note",
        arguments={"text": text, "category": category, "speaker": speaker},
        execute=execute,
    )


#: How long a note may be. This is the rule for *any* caller, which is why it
#: stays here rather than moving to the route schema that also enforces it:
#: ``tests/api.py`` reaches these functions directly, with no adapter in front,
#: and the HTTP door is not the only door.
#:
#: The schema copy in ``web/routes.py`` is not redundant with it. Over HTTP the
#: bound has to fire in the dispatcher, because ``audited_primitive`` journals an
#: attempt's arguments before this function runs — a refusal here has already let
#: the payload onto the disk. ``TestDeclaredBounds`` pins the two equal.
MAX_NOTE_TEXT = 4000


def condition(
    state: EngineState,
    encounter_id: str,
    target: str,
    condition_name: str,
    applied: bool = True,
    levels: int = 1,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Impose or lift a condition on one combatant by the table's ruling.

    Journalled like an action rather than like a note, because it changes the
    fight: a resume that replayed the notes and not this would rebuild a
    different creature.

    And *stamped and checkpointed* like an action, for the same reason and by
    the same three lines :func:`execute_act` uses. This is the third mutator of
    an encounter and the only one that ever skipped them, which left every
    fight where somebody made a ruling exporting a bundle
    ``replay.validate_replay`` refuses — unstamped events, and a
    ``latest_state`` one ruling past the final checkpoint. Recovery had it
    right all along (``sessions.recover_session`` replays this operation with
    both), so the two paths disagreed about the same fight.

    ``levels`` is forwarded to :meth:`Encounter.set_condition` unchanged — it
    only accumulates on a condition the effect row marks ``cumulative``, so a
    GM imposing three levels of an ordinary condition gets the same level-1
    result a caller who never sends it gets.
    """
    # Checked here rather than left to ``Creature.add_condition``'s own floor:
    # that one raises a bare ValueError, which the adapter would answer as a
    # 500. A level a caller typed is bad input and gets said so.
    if levels < 1:
        raise RequestError(
            f"levels is {levels}, and a condition level must be at least 1 — "
            f"a numeric condition effect scales by the level, so one below it "
            f"inverts the effect rather than weakening it"
        )
    session = sessions.session_for(state, encounter_id)

    def execute() -> dict[str, Any]:
        before = len(session.encounter.log)
        try:
            session.encounter.set_condition(
                target, condition_name, applied=applied, levels=levels
            )
        except (EncounterError, UnknownCondition) as error:
            raise RequestError(str(error)) from error
        completed_at = sessions.utc_now()
        session.event_timestamps.extend(
            [completed_at] * (len(session.encounter.log) - before)
        )
        sessions.capture_checkpoint(session, completed_at)
        target_creature = session.encounter.creatures[target]
        return {
            "encounter_id": encounter_id,
            "target": target,
            "condition": condition_name,
            "applied": applied,
            "conditions": sorted(target_creature.conditions),
            "level": target_creature.level_of(condition_name),
        }

    return primitives.audited_primitive(
        state,
        encounter_id=encounter_id,
        request_id=request_id,
        operation="encounter_condition",
        arguments={
            "target": target,
            "condition": condition_name,
            "applied": applied,
            "levels": levels,
        },
        execute=execute,
    )


def event_log(
    state: EngineState,
    encounter_id: str,
    since: int = 0,
    limit: int = 500,
    include_actions: bool = True,
) -> dict[str, Any]:
    session = sessions.session_for(state, encounter_id)
    if since < 0:
        raise RequestError(f"since must not be negative: {since}")
    if limit < 1:
        raise RequestError(f"limit must be at least 1: {limit}")
    events = session.encounter.log
    page = events[since:since + limit]
    result: dict[str, Any] = {
        "encounter_id": encounter_id,
        "seed": session.seed,
        "format": "fivee-sim-log/1",
        "total_events": len(events),
        "since": since,
        "events": [event.as_dict() for event in page],
        "next": since + len(page) if since + len(page) < len(events) else None,
        "total_actions": len(session.encounter.actions),
    }
    if include_actions:
        result["actions"] = [record.as_dict() for record in session.encounter.actions]
    return result


def execute_act(
    state: EngineState,
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
    facing: str | None = None,
    natural: int | list[int] | None = None,
    *,
    actor: str | None = None,
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
    is standing on it. A fixture trigger may move other fixtures automatically;
    maintained triggers refuse contrary manual interactions before spending,
    and automatic transitions bypass the target's reach, cost, and check;
    ``stand`` takes nothing and gets a Prone creature up, spending half its Speed
    from this turn's movement and no action. A position —
    ``to_position``, ``center``, or a ``toward`` point — is ``[x, y]`` in feet on
    the plane; a bare number is accepted and means feet along the x-axis. On a
    battle map a move routes itself around walls and enemies; ``path`` optionally
    pins the exact route as ``[x, y]`` waypoints, one per square. ``to_level``
    ends a move on another storey: walk to a stairway on your own level — the
    square named by ``to_position`` — and it carries you, charging the rise
    between the two floors as a climb. ``movement_mode`` selects walk, climb,
    swim, fly, or burrow; the creature must have that speed, and flight does not need a
    connector. ``facing`` sets where the actor ends up looking, overriding what
    a move would otherwise derive from the leg that ended it; it changes no roll
    and is refused unless it names one of the eight grid directions. Illegal
    actions are refused with the reason rather than silently adjusted.

    ``actor`` names who is taking it, and belongs to an **interlude**: nothing
    there decided an order, so each act opens a fresh beat for the creature it
    names. A fight refuses it — initiative has already answered that question,
    and a second answer beside it would be a quieter turn order.

    The audited path is :func:`act`; this is the step it wraps, kept separate
    so the attempt and result records can bracket exactly the work that moves
    the fight.
    """
    session = sessions.session_for(state, encounter_id)
    try:
        action_kind = ActionKind(kind)
    except ValueError as error:
        allowed = ", ".join(item.value for item in ActionKind)
        raise RequestError(f"kind must be one of: {allowed}") from error
    selected_mode: MovementMode | None = None
    if movement_mode is not None:
        try:
            selected_mode = MovementMode(movement_mode)
        except ValueError as error:
            allowed = ", ".join(mode.value for mode in MovementMode)
            raise RequestError(f"movement_mode must be one of: {allowed}") from error
    waypoints: list[tuple[int, int]] = []
    for step in path or []:
        point = specs.parse_point(step, "each path waypoint")
        if isinstance(point, int):
            raise RequestError("each path waypoint must be an [x, y] pair of feet")
        waypoints.append(point)
    aim_direction: tuple[int, int] | None = None
    if direction is not None:
        parsed = specs.parse_point(direction, "direction")
        if isinstance(parsed, int):
            raise RequestError("direction must be an [x, y] unit offset such as [1, 0]")
        aim_direction = parsed
    aim_toward: str | tuple[int, int] | None = None
    if toward is not None:
        if isinstance(toward, str):
            aim_toward = toward
        else:
            parsed = specs.parse_point(toward, "toward")
            if isinstance(parsed, int):
                raise RequestError("toward must be a combatant name or an [x, y] point")
            aim_toward = parsed
    action = Action(
        kind=action_kind,
        target=target,
        attack=attack,
        item=item,
        spell=spell,
        slot_level=slot_level,
        to_position=(
            specs.parse_point(to_position, "to_position") if to_position is not None else None
        ),
        targets=tuple(targets or ()),
        center=specs.parse_point(center, "center") if center is not None else None,
        direction=aim_direction,
        toward=aim_toward,
        path=tuple(waypoints),
        feature=feature,
        set_open=set_open,
        to_level=to_level,
        movement_mode=selected_mode,
        as_bonus_action=as_bonus_action,
        facing=specs.parse_facing(facing),
        natural=specs.parse_natural(natural),
    )
    try:
        events = session.encounter.act(action, session.rng, actor=actor)
    except (EncounterError, DiceError) as error:
        raise RequestError(str(error)) from error
    completed_at = sessions.utc_now()
    session.event_timestamps.extend([completed_at] * len(events))
    sessions.capture_checkpoint(session, completed_at)
    return {
        "events": [event.as_dict() for event in events],
        "state": session.encounter.state(),
    }


def act(
    state: EngineState,
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
    facing: str | None = None,
    natural: int | list[int] | None = None,
    request_id: str | None = None,
    viewer: str | None = None,
    *,
    actor: str | None = None,
    view: str | None = None,
) -> dict[str, Any]:
    """Take the turn, and answer either the GM or one seat in the fight.

    ``view`` defaults to ``delta`` here and on :func:`advance`, and that is the
    user-visible break this phase exists to make: the two operations a fight
    calls hundreds of times stop re-sending the fight. A caller that wants the
    old answer asks for ``view=full`` and gets it byte for byte.

    An idempotent **retry** is always answered ``full``, whatever was asked for.
    A retry means the caller did not hear the first answer, so the one thing it
    certainly does not hold is the payload a delta would be against.

    ``viewer`` narrows the returned ``state`` and nothing else. What is
    journaled and what an idempotent retry replays stay whole: this fight's
    audit record is the GM's, whichever chair happened to post the action.

    ``actor`` names who acts in an interlude, and is journaled with the rest of
    the arguments: it is an input the chapter cannot re-derive, so a recovery
    that dropped it would replay somebody else's beat.
    """
    chosen = views.parse_view(view, views.DELTA)
    session = sessions.session_for(state, encounter_id)
    cached = sessions.cached_request(session, request_id)
    if cached is not None:
        return _answered(session, cached, viewer, views.FULL)
    # Before anything durable happens, and for the reason ``create`` refuses an
    # unknown ``viewer`` before it starts a fight: a mistyped name is a client's
    # mistake rather than a table's event, and journaling an attempt and a
    # refusal for a creature that does not exist writes a beat nobody took into
    # the record a replay is built from. Only in an interlude, because a fight
    # has a better answer for a named actor — that it takes none at all — and
    # ``404 no combatant named`` would send its caller looking for a typo.
    if actor is not None and session.encounter.mode is EncounterMode.EXPLORATION:
        require_seat(session.encounter.creatures, actor)
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
        "facing": specs.parse_facing(facing),
        # Recorded for the same reason ``natural`` below is: it is an input the
        # engine cannot re-derive. An interlude has no initiative, so an act
        # replayed without its actor is not the act that was taken — it is
        # refused, and the caller reading the refusal sees a complaint about
        # initiative rather than a missing field.
        "actor": actor,
        # Normalised before it is written, like ``facing`` above: a resume reads
        # this dict back through ``specs.action_from_journal``, so what is
        # recorded has to be what the action actually ran with. A face left out
        # here would be re-rolled from the RNG on recovery, and the fight that
        # came back would disagree with the one the caller was told about.
        "natural": list(specs.parse_natural(natural)),
    }
    # Refused before the attempt is written, on the terms ``audited_primitive``
    # states: nothing was rolled, so nothing happened to record, and the journal
    # keeps ``finalized`` as its last word.
    if session.finalized:
        raise RequestError(f"encounter {encounter_id!r} is finalized")
    index, started_at = sessions.attempt_started(
        state, encounter_id, session, "encounter_act", arguments, request_id
    )
    try:
        result = execute_act(
            state,
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
            facing,
            natural,
            actor=actor,
        )
    except (RequestError, EncounterError) as error:
        sessions.attempt_finished(
            state,
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
        raise RequestError(str(error)) from error
    sessions.attempt_finished(
        state,
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
    return _answered(session, result, viewer, chosen)


def execute_advance(
    state: EngineState, encounter_id: str, natural: int | list[int] | None = None
) -> dict[str, Any]:
    """End the current turn and begin the next, rolling any death saves that are due."""
    session = sessions.session_for(state, encounter_id)
    events = session.encounter.advance(session.rng, specs.parse_natural(natural))
    completed_at = sessions.utc_now()
    session.event_timestamps.extend([completed_at] * len(events))
    sessions.capture_checkpoint(session, completed_at)
    return {
        "events": [event.as_dict() for event in events],
        "state": session.encounter.state(),
    }


def advance(
    state: EngineState,
    encounter_id: str,
    natural: int | list[int] | None = None,
    request_id: str | None = None,
    viewer: str | None = None,
    *,
    view: str | None = None,
) -> dict[str, Any]:
    """End this turn and begin the next, answering the GM or one seat.

    ``viewer`` narrows the returned ``state``, and ``view`` decides how much of
    it to repeat — both on the same terms as :func:`act`, including the default
    of ``delta`` and the ``full`` answer to a retry.
    """
    chosen = views.parse_view(view, views.DELTA)
    session = sessions.session_for(state, encounter_id)
    cached = sessions.cached_request(session, request_id)
    if cached is not None:
        return _answered(session, cached, viewer, views.FULL)
    # Recorded for the reason ``act`` records its own: a death save the caller
    # rolled is an input, and a resume that re-rolled it would recover a fight
    # where somebody died who did not.
    arguments: dict[str, Any] = {"natural": list(specs.parse_natural(natural))}
    # Before the attempt is written, on ``act``'s terms and for its reasons.
    if session.finalized:
        raise RequestError(f"encounter {encounter_id!r} is finalized")
    index, started_at = sessions.attempt_started(
        state, encounter_id, session, "encounter_advance", arguments, request_id
    )
    try:
        result = execute_advance(state, encounter_id, natural)
    except (RequestError, EncounterError, DiceError) as error:
        sessions.attempt_finished(
            state,
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
        raise RequestError(str(error)) from error
    sessions.attempt_finished(
        state,
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
    return _answered(session, result, viewer, chosen)


def resume(
    state: EngineState,
    encounter_id: str,
    viewer: str | None = None,
    *,
    view: str | None = None,
) -> dict[str, Any]:
    """Read a fight back from its journal, answering the GM or one seat.

    ``viewer`` narrows the returned ``state``. Worth naming here rather than in
    :func:`act`: this is the one of the four whose state comes from
    :func:`state_of` and so carries ``map_source``, which the projection
    classifies ``NEVER`` and drops with everything else.

    ``view`` defaults to ``full`` for :func:`create`'s reason: this is what a
    caller reaches for when it has *lost* the thread — after a restart, or a
    reload, or a second process — which is exactly when a delta has nothing to
    be against.
    """
    chosen = views.parse_view(view, views.FULL)
    existing = state.sessions.get(encounter_id)
    warning: dict[str, str] | None = None
    recovered = existing is None
    session = existing
    if session is None:
        session, warning = sessions.recover_session(state, encounter_id)
    result: dict[str, Any] = {
        "encounter_id": encounter_id,
        "recovered": recovered,
        "finalized": session.finalized,
        "state": _snapshot_of(state, session),
    }
    if warning is not None:
        result["recovery_warning"] = warning
    return _answered(session, result, viewer, chosen)


def list_encounters(state: EngineState, status: str = "active") -> dict[str, Any]:
    """Every journal on the disk, summarised — two lines apiece, never replayed.

    A listing reports an id, two timestamps, a count and whether the fight is
    over, and every one of those is answered by the ends of the file. It used to
    parse and hash-verify each journal whole to get them, which cost the most on
    exactly the fights that had the most in them.

    Two things follow from reading only the ends, and both are deliberate.

    A journal broken in its *middle* is now listed as ``active`` rather than
    ``corrupt``; only an unreadable file or a malformed first or last line still
    earns that word. Nothing is trusted on the strength of it — ``read`` still
    stands in front of recovery and refuses there, which is where a caller is
    about to act on what the journal says.

    And the status comes from the last record rather than a search for a
    ``finalized`` one, which is sound because finalization is terminal: every
    write refuses a finished fight *before* it journals the attempt. A journal
    this build cannot replay is still listed — see ``_unreplayable`` — so a
    file written by an older one is described on that assumption, which is the
    same best effort as the timestamps beside it.
    """
    if status not in {"active", "finalized", "all"}:
        raise RequestError("status must be active, finalized, or all")
    entries: list[dict[str, Any]] = []
    for path in journal_service.list_journals():
        encounter_id = path.parent.name
        try:
            summary = journal_service.head_and_tail(encounter_id)
        except journal_service.JournalError as error:
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
        if summary is None:
            continue
        finalized = summary.last.get("kind") == "finalized"
        actual_status = "finalized" if finalized else "active"
        if status != "all" and status != actual_status:
            continue
        entries.append(
            {
                "encounter_id": encounter_id,
                "status": actual_status,
                "created_at": summary.first.get("timestamp"),
                "updated_at": summary.last.get("timestamp"),
                "records": summary.records,
                "journal_path": str(path),
            }
        )
    return {"status": status, "encounters": entries}


def prune(apply: bool = False) -> dict[str, Any]:
    """Reclaim the ids a fight was claimed for and never written into.

    ``encounter.create`` claims its id by creating the journal, and every path
    between that claim and the first append can fail — a combatant spec the
    rules refuse, a map that will not parse, a process that dies. The name is
    spent either way, and until now nothing ever gave one back: the reason this
    exists is that a checkout accumulates them silently, one per refused
    creation, for ever.

    A dry run by default because the one thing it cannot see is a creation
    *currently* between its claim and its first append — that id is empty and
    legitimately in use, and reaping it would hand the same name out twice. So
    this is an operator's decision on a quiet engine rather than a reaper on a
    timer, and the default answer is a list to look at.

    It does not reap blobs, and that is not an omission. A journal names a blob
    on its creation record, so an id with no creation record named none; there
    is nothing here that could license removing one. Retiring blobs needs a
    survey of the journals that *do* name them, which is a different operation
    against a different hazard.

    The refusal is translated here like every other journal caller's —
    ``sessions.journal_append``, ``sessions.recover_session`` and
    ``list_encounters`` above all do the same. This one did not, and
    ``JournalError`` is a bare ``ValueError`` rather than a ``RequestError``, so
    a prune that could not finish reached the adapter's catch-all and told an
    operator the engine had a defect. The ids already reclaimed ride in the
    sentence, which is where :func:`~fivee_sim.service.encounter_journal.prune`
    puts them for exactly this reason; the ``reaped`` attribute stays available
    to a caller holding the journal module directly.
    """
    try:
        return {"applied": apply, "encounters": journal_service.prune(apply=apply)}
    except journal_service.JournalError as error:
        raise RequestError(str(error)) from error


def finalize(
    state: EngineState,
    encounter_id: str,
    viewer_link: Callable[[Path], str | None] | None = None,
) -> dict[str, Any]:
    session = sessions.session_for(state, encounter_id)
    if session.finalization_result is not None:
        return deepcopy(session.finalization_result)
    target = replay_path(encounter_id)
    exported = map_ops.replay_export(
        state,
        encounter_id,
        path=str(target),
        format_version=replay_service.LATEST_FORMAT_VERSION,
        viewer_link=viewer_link,
    )
    result = {
        "encounter_id": encounter_id,
        "status": "finalized",
        "replay_path": str(target),
        "bytes": exported["bytes"],
        "sha256": exported["sha256"],
        "journal_path": str(journal_service.journal_path(encounter_id)),
    }
    sessions.journal_append(
        state,
        encounter_id,
        {"kind": "finalized", "timestamp": sessions.utc_now(), "result": result},
        session,
    )
    session.finalized = True
    session.finalization_result = deepcopy(result)
    return result
