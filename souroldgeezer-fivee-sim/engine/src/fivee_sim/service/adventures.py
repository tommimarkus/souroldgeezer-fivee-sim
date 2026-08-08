"""An adventure: an ordered run of encounters, with the party carried between them.

**It is a guarded document, not a journal, and that is the whole of the design.**
An encounter journal exists because a fight takes hundreds of appends and a
process can die between two of them, so it hash-chains every record and keeps a
crash tail. An adventure takes roughly *one* write per encounter, and
:func:`~fivee_sim.service.durable.atomic_write` publishes by ``os.replace`` —
there is no torn prefix for chain machinery to repair. What an adventure does
need is agreement about *which version* a writer started from, and
:func:`~fivee_sim.service.durable.guarded_write` is exactly that: the version
you read is the precondition you write under, and a second writer is refused
rather than merged. ``finalized`` is a field here rather than a record to scan
for, for the same reason.

**It has its own root.** ``adv-<n>.json`` in
:func:`~fivee_sim.paths.adventures_root`. It used to live beside the journals,
kept apart from them by an id grammar anchored on ``enc-`` and a glob that
matched ``enc-*.jsonl`` — two facts in two modules that had to stay true
together, where widening either would have had ``encounter.list`` reporting
adventures as fights in progress. A separate root is the same guarantee with
nothing to keep in step: a listing cannot report the other kind's files because
it cannot reach them.

**This module imports :mod:`~fivee_sim.service.encounters`, and never the
reverse.** An ``adventure_id`` argument on ``encounter.create`` would invert
that and close the cycle; membership is answered from this side, where the
ordered list already lives.

**Recovery is a caller-supplied delta, not simulated rest rules.** There is no
separate interlude operation and no hit dice: :func:`link_encounter` takes an
optional ``recovery`` mapping applied to each combatant's ending state *before*
the carry-over composes it, so "the party takes a long rest" is whatever the
caller says it is. That is the same posture ``analytics/scenario.py`` takes
toward travel — the engine resolves what it can resolve, and does not invent the
rest.

**A run is fights and interludes, and a boundary keeps the ground.** A link
states its ``mode`` — a fight by default — so an adventure can open on a walk
across the mill floor and close on the ambush at the end of it. The party's
squares already crossed a boundary, because ``position`` is in
:data:`CARRIED_STATE_KEYS`; the map never did, so ``carry_map`` is what puts the
next chapter on the same ground. It resolves the previous chapter's map id from
that chapter's **frozen creation record**, for the reason :func:`compose_replay`
reads frozen artifacts — what a chapter was started on is not a question to ask
a live session.

**A run's replay is composed from frozen files, never re-derived.**
:func:`compose_replay` reads each member's ``encounter.finalize`` artifact off
disk and nests it verbatim. It starts no session and replays no action, and that
is a correctness property rather than an optimisation: a fight replayed under
whatever kernel is loaded now can end a hit point away from where it ended when
it was recorded, and with carry-over the *next* chapter's starting state no
longer follows from the previous one's ending state — while the integrity block
hashes the inconsistency happily. A member that was never finalized is refused
by name; a member whose artifact is gone is refused too, never substituted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from .. import __version__
from ..kernel.grid import as_point, square_center
from ..model.encounter import EncounterMode
from ..paths import StorageLayout, adventures_root
from . import durable, encounters, runs, scenes, sessions
from . import encounter_journal as journal_service
from . import replay as replay_service
from .common import canonical_json, sha256_of, slugify
from .errors import NotFoundError, ReplayError, RequestError
from .sessions import EngineState

__all__ = [
    "CARRIED_STATE_KEYS",
    "DOCUMENT_FIELDS",
    "FORMAT",
    "FORMAT_VERSION",
    "LIST_STATUSES",
    "NoCurrentChapterError",
    "RECOVERY_NOTE_MAX",
    "adventure_path",
    "brief_for",
    "carry_forward",
    "compose_replay",
    "create",
    "start",
    "finalize",
    "link_encounter",
    "list_adventures",
    "state_of",
]

#: What the file says it is, so a stray JSON document in the encounters
#: directory is refused rather than half-read as an adventure.
FORMAT = "fivee-sim-adventure"
FORMAT_VERSION = 1

#: What a stored adventure must say about itself, and what a composed replay
#: names it by. One declaration with two readers — :func:`_parsed` refuses a
#: document missing any of them and :func:`compose_replay` carries exactly these
#: into the envelope — so widening the document widens the envelope rather than
#: leaving a run's replay silent about something the run records.
DOCUMENT_FIELDS: tuple[str, ...] = ("id", "name", "created_at", "status")

#: What :func:`list_adventures` filters on, and the authority the route table's
#: own ``status`` enum is held against. Public rather than an inline literal
#: because ``routes.py`` may not import this layer — it stays free of anything
#: that drags in a socket server — so the contract writes the set out and
#: ``TestDeclaredEnums`` holds the two against each other. That is a weaker
#: guarantee than deriving it outright and a strictly stronger one than the
#: exemption ``encounter.list.status`` still carries.
LIST_STATUSES: tuple[str, ...] = ("active", "finalized", "all")

#: A recovery note is caller-authored replay prose, bounded like an encounter
#: note so one chapter boundary cannot become an unbounded adventure document.
RECOVERY_NOTE_MAX = 4000

#: Still anchored on ``adv-``, but no longer for the reason it was: the two
#: kinds have separate roots now and cannot name each other's files whatever the
#: grammars say. What the anchor is left doing is what a path-safety pattern is
#: actually for — refusing ``..``, a separator, or an absolute path in an id
#: that becomes a filename.
_SAFE_ID = re.compile(r"^adv-[A-Za-z0-9_-]+$")

#: The version a file that is not there reports. A sentinel rather than ``None``
#: because :func:`~fivee_sim.service.durable.guarded_write` reads ``None`` as
#: *no precondition at all*, and "this must not exist yet" is a precondition —
#: it is what makes id allocation safe against a second process.
_ABSENT = ""


class NoCurrentChapterError(RequestError):
    """The adventure exists, but has no chapter a live view can follow."""

#: The fields a combatant carries **out of the fight it just finished** and into
#: the next one: everything a turn can change and a spec can state.
#:
#: Every one of these is a key ``Encounter.state()`` reports *and* a key
#: ``creature_from_spec`` accepts, under the same name and the same shape. Two
#: tests hold that in both directions rather than trusting this comment, because
#: a name that matched neither would simply never overlay anything and nothing
#: else would go red.
#:
#: What is deliberately *not* here is as load-bearing. ``initiative`` and
#: ``concentrating_on`` belong to the fight that is over — an initiative roll
#: from the last encounter is not a fact about the next one, and concentration
#: ends with the effect it was holding. ``arrival_round`` is reset rather than
#: carried, because a combatant who joined the last fight on round four is
#: present from the start of this one. And ``attacks`` is the trap: the state
#: payload emits it as a list of *names*, so overlaying it would replace a
#: combatant's whole attack list with strings ``attack_from_spec`` cannot read.
#:
#: ``temp_hp`` carries for the same reason ``hp`` does. SRD 5.2.1, *Temporary
#: Hit Points*: they "last until they're depleted or you finish a Long
#: Rest," and this engine models no rest of its own — dropping the field at
#: every chapter boundary regardless would end a buffer the rules say
#: survives one. A caller stating "they took a long rest" already has the
#: channel to clear it: ``recovery`` (see :func:`link_encounter`) accepts any
#: key this set does, ``temp_hp`` included.
#:
#: ``condition_levels`` carries beside ``conditions`` for the reason it is
#: emitted unconditionally in the first place: this overlay is by *presence*,
#: so a combatant who shed every leveled condition mid-fight reports an empty
#: dict, and that empty dict has to overlay the previous chapter's — otherwise
#: an absent key would leave the old, non-empty capture standing and the
#: combatant would arrive at the next chapter still carrying a level for a
#: condition it no longer holds.
CARRIED_STATE_KEYS: frozenset[str] = frozenset({
    "hp",
    "temp_hp",
    "conditions",
    "condition_levels",
    "death_saves",
    "stable",
    "dead",
    "surrendered",
    "spell_slots",
    "items",
    "position",
    "level",
    "facing",
})


def carry_forward(
    normalized: Mapping[str, Any], latest: Mapping[str, Any]
) -> dict[str, Any]:
    """One combatant's captured creation input, overlaid with how it ended up.

    ``normalized`` is the ``normalized_combatant_payload`` the journal captured
    at creation — complete creation input, every key
    ``specs.DESCRIBED_SPEC_KEYS`` accepts. ``latest`` is that combatant's entry
    in ``Encounter.state()``.

    The base is the *capture*, never the state, and that direction is the point.
    ``_creature_state`` emits ``attacks`` as bare names and emits no
    ``abilities``, ``save_bonuses``, ``resistances``, ``spell_save_dc`` or
    ``size`` at all — so a projection from state alone rebuilds a creature with
    no attacks and default statistics, and nothing refuses it, because not one
    of those keys is required. The state's job here is narrower and it is the
    job only it can do: say what the fight *changed*.

    The overlay is by presence, not by default. A key the state payload omits —
    ``level`` on a fight with no map — leaves the captured value alone rather
    than being read as ``None``. ``facing`` used to be the other example here
    and is not one any more: it is reported unconditionally, so a combatant
    nobody tracked carries its ``None`` forward, which is the same answer the
    capture already held.
    """
    spec = deepcopy(dict(normalized))
    for key in CARRIED_STATE_KEYS:
        if key in latest:
            spec[key] = deepcopy(latest[key])
    # Everybody who walks into the next encounter is there when it starts.
    spec["arrival_round"] = 1
    return spec


# --- the document -----------------------------------------------------------
def _adventures_dir(state: EngineState | None) -> Path:
    if state is not None and state.storage is not None:
        return state.storage.adventures_dir
    return adventures_root()


def adventure_path(adventure_id: str, state: EngineState | None = None) -> Path:
    """Where ``adventure_id``'s document lives, whether or not it exists yet.

    An id outside the grammar is *not found* rather than malformed, which is the
    rule ``maps.path_for_id`` follows and for the same reason: a traversal id
    cannot name a file here, so there is nothing to diagnose beyond its absence.
    """
    if _SAFE_ID.fullmatch(adventure_id) is None:
        raise NotFoundError(f"no adventure {adventure_id!r}")
    if state is not None and state.storage is not None:
        selected = state.storage.run_id
        if selected is not None and selected != "legacy":
            if selected.startswith("run-"):
                manifest = runs.state_of(selected, runs_dir=state.storage.runs_dir)
                if manifest["adventure_id"] != adventure_id:
                    raise NotFoundError(
                        f"no adventure {adventure_id!r} in run {selected!r}"
                    )
            elif selected != adventure_id:
                raise NotFoundError(f"no adventure {adventure_id!r} in run {selected!r}")
    if state is not None and state.storage is not None and state.storage.run_id is None:
        return (
            state.storage.runs_dir
            / adventure_id
            / "adventures"
            / f"{adventure_id}.json"
        )
    return _adventures_dir(state) / f"{adventure_id}.json"


def _files(state: EngineState | None = None) -> list[Path]:
    """Every adventure document here, and nothing that merely looks like one.

    ``adv-*.json`` is a wider net than an adventure id: ``adv-1.replay.json`` is
    a *composed replay of* ``adv-1`` and matches the glob. Every reader of this
    listing would then be wrong about it — ``list_adventures`` reports it as a
    corrupt adventure, and ``_known`` offers ``adv-1.replay`` to a lost caller as
    somewhere else to look. Filtered by the same grammar
    :func:`adventure_path` builds a name with, so what counts as an adventure
    file is decided in one place, and a stem with a dot in it is not an id this
    module could ever have produced.

    A separate root does not answer this one, which is why the filter survived
    the move: ``adventure.replay`` writes wherever the caller names, and a
    caller may perfectly reasonably name this directory. What the root settled
    is only that a *journal* can no longer be mistaken for an adventure, because
    the two kinds are no longer in reach of one listing.
    """
    if state is not None and state.storage is not None and state.storage.run_id is None:
        return sorted(
            path
            for path in state.storage.runs_dir.glob("adv-*/adventures/adv-*.json")
            if _SAFE_ID.fullmatch(path.stem) is not None
            and path.parent.parent.name == path.stem
        )
    root = _adventures_dir(state)
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.glob("adv-*.json")
        if _SAFE_ID.fullmatch(path.stem) is not None
    )


def _render(document: Mapping[str, Any]) -> str:
    """The document's canonical text — what its version is the hash of."""
    return json.dumps(dict(document), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _current_version(path: Path) -> str:
    """The version on disk, or :data:`_ABSENT` when there is no file yet.

    Read as bytes rather than re-rendered: every write here publishes canonical
    text, so the two agree, and hashing what is actually there means a caller's
    precondition is checked against the file rather than against our idea of it.
    """
    try:
        return sha256_of(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _ABSENT
    except (OSError, UnicodeDecodeError):
        return "unreadable"


def _known(state: EngineState | None = None) -> str:
    return ", ".join(sorted(path.stem for path in _files(state))) or "none"


def _load(
    adventure_id: str, state: EngineState | None = None
) -> tuple[dict[str, Any], str]:
    """One adventure document and the version a write must match."""
    path = adventure_path(adventure_id, state)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise NotFoundError(
            f"no adventure {adventure_id!r}; adventures here: {_known(state)}"
        ) from None
    except (OSError, UnicodeDecodeError) as error:
        raise RequestError(f"cannot read {path}: {error}") from error
    document = _parsed(text, path)
    return document, sha256_of(text)


def _parsed(text: str, path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise RequestError(f"{path} is not valid JSON: {error.msg}") from error
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise RequestError(f"{path} is not a {FORMAT} document")
    if payload.get("format_version") != FORMAT_VERSION:
        raise RequestError(
            f"{path} is format_version {payload.get('format_version')!r}; "
            f"this engine reads {FORMAT_VERSION}"
        )
    for key in DOCUMENT_FIELDS:
        if not isinstance(payload.get(key), str):
            raise RequestError(f"{path} has no {key!r}")
    if not isinstance(payload.get("members"), list):
        raise RequestError(f"{path} has no 'members'")
    if not isinstance(payload.get("request_ids"), dict):
        raise RequestError(f"{path} has no 'request_ids'")
    # Every member records which kind of chapter it is, and one written before
    # there was a second kind was a fight. Filled in on the way *in* rather than
    # at each of the four places a member is reported, so nothing downstream has
    # to ask which engine wrote the document it is holding. Nothing is rewritten
    # here: the version a write is guarded by is the hash of the file's own
    # text, so the next ordinary link is what quietly brings the file forward.
    for index, member in enumerate(payload["members"]):
        if not isinstance(member, dict):
            raise RequestError(f"{path} member {index} is not a record")
        member.setdefault("mode", EncounterMode.COMBAT.value)
    return payload


def _write(
    adventure_id: str,
    document: Mapping[str, Any],
    *,
    expected: str,
    state: EngineState | None = None,
) -> str:
    """Publish the document, refusing a version somebody else has moved past."""
    path = adventure_path(adventure_id, state)
    text = _render(document)
    durable.guarded_write(
        path,
        lambda: text,
        expected=expected,
        # Read under the lock, never before it: computing this first is exactly
        # the race the precondition exists to close.
        current=lambda: _current_version(path),
        subject=f"the adventure {adventure_id!r}",
    )
    return sha256_of(text)


def _precondition(expected_version: str | None, read_at: str) -> str:
    """The version this write is guarded by, which is never *no version*.

    ``map.edit``'s rule, for ``map.edit``'s reason: opt-in protection protects
    nobody. A link reads the members, appends one, and writes the whole document
    back, so an unguarded write would let two callers each be told they linked
    and leave one encounter missing from a run that acknowledged it. A caller who
    passes nothing — or ``*``, which the adapter turns into nothing — is guarded
    by the version *this call* read a moment ago.
    """
    return read_at if expected_version is None or expected_version == "*" else expected_version


def _recorded(
    document: Mapping[str, Any],
    request_id: str,
    operation: str,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """What a previous call under this key did, if it was *this* operation.

    A request id is a string the caller chose, and nothing stops one being used
    for two different operations. Matching the key alone would let a retried
    creation be answered with whatever adventure a link happened to record under
    the same string, so a key already spent elsewhere is refused rather than
    answered — a caller who reuses one is making a mistake that idempotency
    cannot silently fix.
    """
    found = document["request_ids"].get(request_id)
    if not isinstance(found, Mapping):
        return None
    sessions.ensure_idempotency_identity(request_id, found, operation, arguments)
    return found


def _refuse_if_stale(
    adventure_id: str,
    expected_version: str | None,
    state: EngineState | None = None,
) -> None:
    """Refuse a caller's version early, before anything durable is written."""
    if expected_version is None or expected_version == "*":
        return
    current = _current_version(adventure_path(adventure_id, state))
    if expected_version != current:
        raise durable.StaleWriteError(
            f"the adventure {adventure_id!r}", expected=expected_version, current=current
        )


def _response(document: Mapping[str, Any], version: str) -> dict[str, Any]:
    return {**deepcopy(dict(document)), "version": version}


# --- operations --------------------------------------------------------------
def create(
    name: str,
    request_id: str | None = None,
    *,
    state: EngineState | None = None,
) -> dict[str, Any]:
    """Start an adventure: a named, empty, ordered run of encounters."""
    titled = name.strip()
    if not titled:
        raise RequestError("adventure name must not be blank")
    storage = None if state is None else state.storage
    if storage is not None and storage.run_id is not None:
        if storage.run_id == "legacy":
            raise RequestError("adventure.create cannot write: legacy storage is read-only")
        raise RequestError(
            "adventure.create allocates a new run; omit --run for this operation"
        )
    if request_id is not None:
        existing = _by_request_id(request_id, {"name": titled}, state)
        if existing is not None:
            return existing
    if storage is not None:
        try:
            storage.runs_dir.mkdir(parents=True, exist_ok=True)
            with durable.file_lock(storage.runs_dir / ".allocation"):
                if request_id is not None:
                    existing = _by_request_id(request_id, {"name": titled}, state)
                    if existing is not None:
                        return existing
                return _create_new(titled, request_id, state)
        except OSError as error:
            raise RequestError(
                f"cannot allocate an adventure run under {storage.runs_dir}: {error}"
            ) from error
    return _create_new(titled, request_id, state)


def start(name: str, opening: Mapping[str, Any], request_id: str | None = None,
          *, state: EngineState) -> dict[str, Any]:
    """Atomically publish a run, adventure document, and chapter-zero fight."""
    if state.storage is None or state.storage.run_id is not None:
        raise RequestError("adventure.start allocates a new run; omit --run for this operation")
    titled = name.strip()
    scene_id, party = opening.get("scene_id"), opening.get("party")
    if not titled:
        raise RequestError("adventure name must not be blank")
    if not isinstance(scene_id, str) or not isinstance(party, list):
        raise RequestError("opening requires scene_id and party")
    identity = {"name": titled, "opening": deepcopy(dict(opening))}

    def initialize(stage: Path, _run_id: str) -> tuple[str, Any]:
        adventure_id = _next_free_id(state)
        workspace = stage / adventure_id
        for part in ("maps", "scenes", "replays", "encounters", "adventures", "blobs"):
            (workspace / part).mkdir(parents=True, exist_ok=True)
        prior = state.storage
        state.storage = StorageLayout(
            run_id=adventure_id, runs_dir=stage, runtime_dir=prior.runtime_dir,
            shared_map_paths=prior.shared_map_paths, shared_replay_paths=prior.shared_replay_paths,
            shared_scenes_dir=prior.shared_scenes_dir,
            legacy_encounters_dir=prior.legacy_encounters_dir,
            legacy_adventures_dir=prior.legacy_adventures_dir,
            legacy_blobs_dir=prior.legacy_blobs_dir,
        )
        created: dict[str, Any] | None = None
        try:
            scene = scenes.load(scene_id, root=prior.shared_scenes_dir)["document"]
            roster, map_spec, map_id = _opening_roster(state, scene, party)
            created = encounters.create(
                state, roster, opening.get("seed", scene.get("seed")),
                str(scene.get("movement_rule", "5-5-5")), map_spec, map_id, request_id,
                mode=str(scene.get("mode", EncounterMode.COMBAT.value)),
            )
            member = {"index": 0, "encounter_id": str(created["encounter_id"]),
                      "linked_at": sessions.utc_now(), "carried": [],
                      "mode": str(created["state"]["mode"])}
            document: dict[str, Any] = {
                "format": FORMAT, "format_version": FORMAT_VERSION, "id": adventure_id,
                "name": titled, "created_at": sessions.utc_now(), "status": "active",
                "members": [member], "request_ids": {},
            }
            if request_id is not None:
                document["request_ids"][request_id] = {
                    "operation": "adventure.start",
                    "idempotency_fingerprint": sessions.idempotency_fingerprint(
                        "adventure.start", identity), **member,
                }
            version = _write(adventure_id, document, expected=_ABSENT, state=state)
            return adventure_id, {"adventure": document, "version": version, "encounter": created}
        except BaseException:
            if created is not None:
                state.sessions.pop(str(created["encounter_id"]), None)
            raise
        finally:
            state.storage = prior

    published = runs.create(request_id, identity, initialize, "adventure.start",
                            runs_dir=state.storage.runs_dir)
    bound = str(published["adventure_id"])
    data = published.get("initialized")
    if not isinstance(data, Mapping):
        path = state.storage.runs_dir / str(published["id"]) / "adventures" / f"{bound}.json"
        text = path.read_text(encoding="utf-8")
        document = _parsed(text, path)
        temporary = state.storage
        state.storage = StorageLayout(
            run_id=bound, runs_dir=path.parent.parent.parent, runtime_dir=temporary.runtime_dir,
            shared_map_paths=temporary.shared_map_paths,
            shared_replay_paths=temporary.shared_replay_paths,
            shared_scenes_dir=temporary.shared_scenes_dir,
            legacy_encounters_dir=temporary.legacy_encounters_dir,
            legacy_adventures_dir=temporary.legacy_adventures_dir,
            legacy_blobs_dir=temporary.legacy_blobs_dir,
        )
        try:
            encounter_id = str(document["members"][0]["encounter_id"])
            created = encounters.creation_response(
                state, encounter_id, sessions.session_for(state, encounter_id))
        finally:
            state.storage = temporary
        data = {"adventure": document, "version": sha256_of(text), "encounter": created}
    member = data["adventure"]["members"][0]
    return {"run_id": published["id"], "adventure_id": bound,
            "encounter_id": member["encounter_id"], "chapter_index": 0,
            "run": {key: value for key, value in published.items() if key != "initialized"},
            "adventure": deepcopy(data["adventure"]), "version": data["version"],
            "encounter": data["encounter"]}


def _opening_roster(state: EngineState, scene: Mapping[str, Any], party: list[Any]
                    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    map_spec, map_id = scene.get("map"), scene.get("map_id")
    if map_spec is None and map_id is None:
        raise RequestError("opening scene requires a map")
    if map_spec is not None and map_id is not None:
        raise RequestError("opening scene names both map and map_id")
    checked = []
    for member in party:
        if not isinstance(member, Mapping) or member.get("team") != "party":
            raise RequestError("every opening party member must have team 'party'")
        checked.append(deepcopy(dict(member)))
    resolved = sessions.resolve_battle_map(
        state, map_spec if isinstance(map_spec, dict) else None,
        map_id if isinstance(map_id, str) else None)
    if resolved is None:
        raise RequestError("opening scene requires a map")
    planes = [(resolved.document.features, 0)] + [
        (level.features, level.index) for level in resolved.document.levels.values()
        if level.index != 0]
    hints, seen = [], set()
    for features, level in planes:
        for feature in features:
            if feature.kind == "spawn" and feature.team == "party":
                hint = (feature.at, level)
                if hint in seen:
                    raise RequestError("opening map has duplicate party spawn positions")
                seen.add(hint)
                hints.append(hint)
    cast = [deepcopy(dict(entry)) for entry in scene.get("combatants", [])
            if isinstance(entry, Mapping) and entry.get("team") != "party"]
    occupied = {
        (as_point(entry["position"] if isinstance(entry["position"], int)
                  else tuple(entry["position"])), int(entry.get("level", 0)))
        for entry in cast
        if isinstance(entry.get("position"), (int, list))
    }
    free = [hint for hint in hints if (tuple(square_center(hint[0])), hint[1]) not in occupied]
    if len(free) < len(checked):
        raise RequestError("opening map has insufficient free party spawn positions")
    for member, (position, level) in zip(checked, free, strict=False):
        member["position"], member["level"] = list(square_center(position)), level
    inline = deepcopy(map_spec) if isinstance(map_spec, Mapping) else None
    return [*checked, *cast], inline, map_id if isinstance(map_id, str) else None


def _create_new(
    titled: str, request_id: str | None, state: EngineState | None
) -> dict[str, Any]:
    """Allocate and initialize one run while the allocation lock is held."""
    # Allocate by trying: the id is only free until somebody else takes it, and
    # a write guarded on "nothing is there" is what turns that race into a retry
    # instead of an overwrite. The bound is a defect guard, not a policy.
    for _attempt in range(64):
        adventure_id, run_root = _allocate_id(state)
        document: dict[str, Any] = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "id": adventure_id,
            "name": titled,
            "created_at": sessions.utc_now(),
            "status": "active",
            "members": [],
            "request_ids": (
                {} if request_id is None else {
                    request_id: {
                        "operation": "adventure.create",
                        "idempotency_fingerprint": sessions.idempotency_fingerprint(
                            "adventure.create", {"name": titled}
                        ),
                    }
                }
            ),
        }
        try:
            if run_root is None:
                version = _write(adventure_id, document, expected=_ABSENT, state=state)
            else:
                for name_part in (
                    "maps", "scenes", "replays", "encounters", "adventures", "blobs"
                ):
                    (run_root / name_part).mkdir()
                path = run_root / "adventures" / f"{adventure_id}.json"
                rendered = _render(document)

                def render_run_document(value: str = rendered) -> str:
                    return value

                def current_run_version(target: Path = path) -> str:
                    return _current_version(target)

                durable.guarded_write(
                    path,
                    render_run_document,
                    expected=_ABSENT,
                    current=current_run_version,
                    subject=f"the adventure {adventure_id!r}",
                )
                version = sha256_of(rendered)
        except durable.StaleWriteError:
            continue
        except OSError as error:
            raise RequestError(
                f"could not initialize adventure run {adventure_id!r}: {error}"
            ) from error
        return _response(document, version)
    raise RequestError("could not allocate an adventure id; too many exist here")


def _next_free_id(state: EngineState | None = None) -> str:
    used = {path.stem for path in _files(state)}
    if state is not None and state.storage is not None:
        used.update(path.name for path in state.storage.runs_dir.glob("adv-*") if path.is_dir())
        legacy = state.storage.legacy_adventures_dir
        if legacy.is_dir():
            used.update(
                path.stem for path in legacy.glob("adv-*.json")
                if _SAFE_ID.fullmatch(path.stem) is not None
            )
    index = 1
    while f"adv-{index}" in used:
        index += 1
    return f"adv-{index}"


def _allocate_id(state: EngineState | None) -> tuple[str, Path | None]:
    """Claim a run directory, or retain the legacy document claim for tests."""
    if state is None or state.storage is None:
        return _next_free_id(state), None
    runs = state.storage.runs_dir
    try:
        runs.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RequestError(f"cannot create adventure runs root {runs}: {error}") from error
    for _attempt in range(10_000):
        adventure_id = _next_free_id(state)
        root = runs / adventure_id
        try:
            root.mkdir()
        except FileExistsError:
            continue
        except OSError as error:
            raise RequestError(f"cannot allocate adventure run {root}: {error}") from error
        return adventure_id, root
    raise RequestError("could not allocate an adventure id; too many exist here")


def _by_request_id(
    request_id: str,
    arguments: Mapping[str, Any],
    state: EngineState | None = None,
) -> dict[str, Any] | None:
    """The adventure a previous call under this key created, if any.

    The same scan ``encounters.creation_request`` does over journals, and for
    the same reason: before the document exists there is nowhere else the key
    could have been recorded.

    Matched on the recorded *operation* as well as the key. A key is the
    caller's to choose and nothing stops one being reused across operations, so
    a bare key match would let a retried creation answer with the adventure some
    earlier link happened to record under the same string.
    """
    for path in _files(state):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            document = _parsed(text, path)
        except RequestError:
            continue
        recorded = document["request_ids"].get(request_id)
        if isinstance(recorded, Mapping):
            sessions.ensure_idempotency_identity(
                request_id, recorded, "adventure.create", arguments
            )
            return _response(document, sha256_of(text))
    return None


def state_of(
    adventure_id: str, *, state: EngineState | None = None
) -> dict[str, Any]:
    """One adventure, whole, with the version a write must match.

    Deliberately no journal reads: an adventure names its members, and what each
    of those fights is currently doing is ``encounter.state``'s answer about
    that fight rather than a summary this operation would have to keep fresh.
    """
    document, version = _load(adventure_id, state)
    return _response(document, version)


def brief_for(
    state: EngineState, adventure_id: str, as_name: str
) -> tuple[dict[str, Any], str]:
    """The current chapter as one seat may see it, plus its composite version.

    The response is an allowlist rather than a redaction of the adventure
    document.  Only the last linked member identifies the current chapter; its
    encounter is projected through :func:`encounters.brief_for`, so this layer
    never reconstructs player visibility or inherits a new adventure field by
    accident.

    The returned version hashes this allowlisted payload itself.  A polling
    client therefore wakes for a visible chapter or encounter change without
    gaining a timing oracle for hidden journal activity.
    """
    document, _adventure_version = _load(adventure_id, state)
    members = document["members"]
    if not members:
        raise NoCurrentChapterError(
            f"adventure {adventure_id!r} has no current chapter; link an encounter first"
        )
    member = members[-1]
    encounter_id = str(member["encounter_id"])
    session = sessions.session_for(state, encounter_id)
    player_state = encounters.brief_for(state, encounter_id, as_name)
    chapter: dict[str, Any] = {
        "index": int(member["index"]),
        "encounter_id": encounter_id,
        "mode": str(member["mode"]),
        "finalized": session.finalized,
    }
    payload = {
        "adventure": {
            "id": str(document["id"]),
            "name": str(document["name"]),
            "status": str(document["status"]),
            "chapter_count": len(members),
        },
        "chapter": chapter,
        "state": player_state,
    }
    version = sha256_of(canonical_json(payload))
    return payload, version


def list_adventures(
    status: str = "active", *, state: EngineState | None = None
) -> dict[str, Any]:
    """Every adventure on disk, without loading the fights they name."""
    if status not in LIST_STATUSES:
        raise RequestError("status must be active, finalized, or all")
    entries: list[dict[str, Any]] = []
    for path in _files(state):
        try:
            document = _parsed(path.read_text(encoding="utf-8"), path)
        except (OSError, UnicodeDecodeError, RequestError) as error:
            if status == "all":
                entries.append(
                    {
                        "adventure_id": path.stem,
                        "status": "corrupt",
                        "problem": str(error),
                        "path": str(path),
                    }
                )
            continue
        actual = str(document["status"])
        if status != "all" and status != actual:
            continue
        entries.append(
            {
                "adventure_id": str(document["id"]),
                "name": str(document["name"]),
                "status": actual,
                "created_at": str(document["created_at"]),
                "encounters": len(document["members"]),
                # The shape of the run, in order, from a listing that opens no
                # journal and no replay artifact: walk, fight, walk.
                "modes": [str(member["mode"]) for member in document["members"]],
                "path": str(path),
            }
        )
    if (
        status == "all"
        and state is not None
        and state.storage is not None
        and state.storage.run_id is None
        and state.storage.runs_dir.is_dir()
    ):
        complete = {str(entry["adventure_id"]) for entry in entries}
        for run in sorted(state.storage.runs_dir.glob("adv-*")):
            if not run.is_dir() or run.name in complete:
                continue
            entries.append(
                {
                    "adventure_id": run.name,
                    "status": "incomplete",
                    "problem": "run allocation has no complete adventure document",
                    "path": str(run),
                }
            )
    return {"status": status, "adventures": entries}


def finalize(
    adventure_id: str,
    expected_version: str | None = None,
    *,
    state: EngineState | None = None,
) -> dict[str, Any]:
    """Close the run: no further encounter may be linked to it.

    Idempotent by reading the field rather than by keeping a key: an adventure
    that is already finalized is answered with the document as it stands, and
    nothing is rewritten — a second call must not move the version a caller is
    holding.
    """
    if state is not None:
        sessions.require_run_write(state, "adventure.finalize")
    document, version = _load(adventure_id, state)
    if document["status"] == "finalized":
        return _response(document, version)
    document["status"] = "finalized"
    written = _write(
        adventure_id,
        document,
        expected=_precondition(expected_version, version),
        state=state,
    )
    return _response(document, written)


def compose_replay(
    adventure_id: str,
    path: str | None = None,
    *,
    state: EngineState | None = None,
) -> dict[str, Any]:
    """The whole run as one replay: every member's frozen bundle, in order.

    Pure file work. Each chapter is the artifact ``encounter.finalize`` wrote,
    read from :func:`~fivee_sim.service.encounters.replay_path` and nested
    verbatim — no session is started, nothing is replayed, and no fight is
    re-derived. See this module's docstring for why that is correctness rather
    than economy.

    The result **always names a file**, never an inline bundle: one realistic v2
    bundle already exceeds the ceiling ``replay_export`` inlines under, and an
    envelope holds several. It lands in the replays directory beside ordinary
    exports unless ``path`` says otherwise — and it is deliberately *not* offered
    as a viewer link, because ``list_replays`` filters on the replay format and
    would never find it.

    The envelope goes through
    :func:`~fivee_sim.service.replay.validate_adventure_replay` before a byte is
    published, which is also what makes that validator's live caller more than a
    dispatch: a member artifact somebody corrupted on disk is refused with the
    diagnostics naming the chapter, rather than published inside a run's replay.
    """
    if state is not None:
        sessions.require_run_write(state, "adventure.replay")
    document, _version = _load(adventure_id, state)
    members = [dict(member) for member in document["members"]]
    if not members:
        raise RequestError(
            f"adventure {adventure_id!r} has no encounters to compose; "
            f"link one and finalize it first"
        )
    chapters = [_chapter(adventure_id, member, state) for member in members]
    bundle = replay_service.adventure_replay_bundle(
        engine_version=__version__,
        adventure={key: document[key] for key in DOCUMENT_FIELDS},
        chapters=chapters,
    )
    diagnostics = replay_service.validate_adventure_replay(bundle)
    if diagnostics:
        raise ReplayError(
            f"the composed replay of adventure {adventure_id!r} is not playable: "
            f"{len(diagnostics)} problem(s)",
            diagnostics,
        )
    serialized = replay_service.serialize_bundle(bundle)
    replay_root = (
        state.storage.replays_dir
        if state is not None and state.storage is not None
        else replay_service.replays_root()
    )
    target = (
        Path(path).expanduser()
        if path is not None
        else replay_root / f"{slugify(str(document['name']))}-{adventure_id}.json"
    )
    try:
        replay_service.atomic_write_text(target, serialized)
    except OSError as error:
        raise RequestError(f"cannot write {target}: {error}") from error
    return {
        "adventure_id": adventure_id,
        "format": replay_service.ADVENTURE_FORMAT,
        "format_version": replay_service.ADVENTURE_FORMAT_VERSION,
        "chapters": len(chapters),
        "encounters": [str(chapter["encounter_id"]) for chapter in chapters],
        "path": str(target),
        "bytes": len(serialized.encode("utf-8")),
        "sha256": replay_service.sha256_bytes(serialized.encode("utf-8")),
    }


def _chapter(
    adventure_id: str,
    member: Mapping[str, Any],
    state: EngineState | None = None,
) -> dict[str, Any]:
    """One chapter: the run's record of a link, wearing the fight it froze.

    ``mode`` is taken from the **bundle** and not from the member record beside
    it, which is the same rule the whole composer follows applied to one field:
    the artifact is what happened, and a document is what somebody wrote down
    about it. A run linked under an engine that did not record the mode still
    composes into an envelope that says which kind each chapter was, because the
    artifacts do — and where the artifact is silent too, every fight frozen
    before there was a second kind was a fight.
    """
    bundle = _frozen_bundle(adventure_id, member, state)
    encounter = bundle.get("encounter")
    frozen_mode = encounter.get("mode") if isinstance(encounter, Mapping) else None
    return {
        **member,
        "mode": (
            str(frozen_mode)
            if isinstance(frozen_mode, str)
            else str(member.get("mode", EncounterMode.COMBAT.value))
        ),
        "replay": bundle,
    }


def _frozen_bundle(
    adventure_id: str,
    member: Mapping[str, Any],
    state: EngineState | None = None,
) -> dict[str, Any]:
    """One member's replay artifact, exactly as ``finalize`` left it.

    An absent file is one refusal rather than two, and deliberately so: the
    artifact's existence *is* the finalization mark on this side of the engine,
    so "never finalized" and "finalized, then the file was removed" are
    indistinguishable from here without re-reading and re-verifying a whole
    hash-chained journal. The remedy is the same either way — finalize the
    encounter, which re-exports it — so the message says that and names both the
    fight and the file it looked for.
    """
    encounter_id = str(member["encounter_id"])
    path = encounters.replay_path(encounter_id, state)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RequestError(
            f"encounter {encounter_id!r} of adventure {adventure_id!r} has no "
            f"finalized replay at {path}; finalize it before composing the run"
        ) from None
    except (OSError, UnicodeDecodeError) as error:
        raise RequestError(f"cannot read {path}: {error}") from error
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise RequestError(f"{path} is not valid JSON: {error.msg}") from error
    if not isinstance(payload, dict):
        raise RequestError(f"{path} is not a replay bundle")
    return payload


def link_encounter(
    state: EngineState,
    adventure_id: str,
    combatants: list[dict[str, Any]] | None = None,
    carry: list[str] | None = None,
    recovery: dict[str, Any] | None = None,
    seed: int | None = None,
    movement_rule: str = "5-5-5",
    map_spec: dict[str, Any] | None = None,
    map_id: str | None = None,
    request_id: str | None = None,
    expected_version: str | None = None,
    *,
    mode: str = EncounterMode.COMBAT.value,
    carry_map: bool = False,
    recovery_note: str | None = None,
) -> dict[str, Any]:
    """Start the adventure's next encounter, carrying the last one's cast into it.

    ``carry`` names who comes forward from the previous encounter, in the order
    they should be built; omitted, it is everybody that fight had. Omitting it
    is the honest default rather than a filter on survivors: a killed combatant
    carries ``dead`` and a stabilised one carries ``stable``, so "the whole cast,
    exactly as the fight left them" is a statement the next encounter can
    actually make, and a caller who wants less says so by name.

    ``combatants`` are the new arrivals, appended after the carried ones and
    written as ordinary creation specs. On the first encounter of an adventure
    there is nothing to carry, so they are the whole roster.

    ``recovery`` maps a carried combatant's name to a partial delta over
    :data:`CARRIED_STATE_KEYS`, applied to their ending state before the
    carry-over composes it. That is where "they took a long rest" lives, and it
    is deliberately the caller's to state.

    ``recovery_note`` is the caller's plain-language name for that boundary,
    such as ``"Long rest at the abbey"``. It requires ``recovery`` but the
    recovery may be empty: a rest that changed no carried value is still a
    fact the replay can show. The note is never interpreted as a rules input.

    ``mode`` says which kind of chapter this one is — a fight by default, so a
    link written before interludes existed starts what it always started. It is
    ``encounter.create``'s to validate and not restated here.

    ``carry_map`` puts this chapter on the ground the previous one was on. The
    party's *squares* have always crossed a boundary — ``position`` is in
    :data:`CARRIED_STATE_KEYS` — but the map never did, so an ambush on the
    floor somebody just walked across meant restating the id and a mistyped one
    silently moved the fight. It is explicit rather than a default because
    omitting a map means theatre of the mind, and that has to keep meaning what
    it means; it is refused alongside an explicit ``map`` or ``map_id``, because
    a link that named two grounds has no correct answer.

    The ``request_id`` is honoured on both halves of the operation: it is
    recorded here so a retry answers from the document without linking twice,
    *and* passed to ``encounter.create``, so a retry after a crash between the
    two writes re-finds the encounter that was already made rather than
    orphaning it.
    """
    sessions.require_run_write(state, "adventure.encounter")
    idempotency_arguments = {
        "combatants": combatants,
        "carry": carry,
        "recovery": recovery,
        "seed": seed,
        "movement_rule": movement_rule,
        "map_spec": map_spec,
        "map_id": map_id,
        "mode": mode,
        "carry_map": carry_map,
        "recovery_note": recovery_note,
    }
    document, version = _load(adventure_id, state)
    if request_id is not None:
        recorded = _recorded(
            document,
            request_id,
            "adventure.encounter",
            idempotency_arguments,
        )
        if recorded is not None:
            return _link_response(state, document, version, recorded)
    if document["status"] != "active":
        raise RequestError(
            f"adventure {adventure_id!r} is {document['status']}; "
            f"start another one to keep playing"
        )
    checked_note = _checked_recovery_note(recovery, recovery_note)
    # Checked here as well as under the lock, and the duplication is the point:
    # the guarded write below is what makes the precondition *sound*, but it
    # runs after the fight has been created, so a caller holding an old version
    # would leave an orphan journal in no run at all before being refused. This
    # catches the ordinary case — somebody read the adventure a while ago —
    # before anything durable happens. The window a concurrent writer can still
    # slip into stays open, and an orphan encounter is the cost: it is a whole,
    # replayable fight that simply belongs to no adventure, not lost data.
    _refuse_if_stale(adventure_id, expected_version, state)
    members: list[dict[str, Any]] = list(document["members"])
    if carry_map:
        # Resolved before the fight is created, like every other refusal above
        # it: a link that started its encounter and only then found there was no
        # map to carry would leave a whole journal belonging to no run at all.
        map_id = _carried_map_id(state, adventure_id, members, map_spec, map_id)
    carried, checked_recovery = _carried_specs(
        state, adventure_id, members, carry, recovery
    )
    roster = [*carried, *(dict(entry) for entry in (combatants or []))]
    created = encounters.create(
        state,
        roster,
        seed,
        movement_rule,
        map_spec,
        map_id,
        request_id,
        mode=mode,
    )
    member = {
        "index": len(members),
        "encounter_id": str(created["encounter_id"]),
        "linked_at": sessions.utc_now(),
        "carried": [str(spec["name"]) for spec in carried],
        # What was built rather than what was asked for, the same direction the
        # creation journal record takes it in.
        "mode": str(created["state"]["mode"]),
    }
    if checked_recovery is not None:
        member["recovery"] = deepcopy(checked_recovery)
        if checked_note is not None:
            member["recovery_note"] = checked_note
    document["members"] = [*members, member]
    if request_id is not None:
        document["request_ids"] = {
            **document["request_ids"],
            request_id: {
                "operation": "adventure.encounter",
                "idempotency_fingerprint": sessions.idempotency_fingerprint(
                    "adventure.encounter", idempotency_arguments
                ),
                **member,
            },
        }
    written = _write(
        adventure_id,
        document,
        expected=_precondition(expected_version, version),
        state=state,
    )
    return {
        "adventure_id": adventure_id,
        "encounter_id": member["encounter_id"],
        "index": member["index"],
        "carried": member["carried"],
        "version": written,
        "adventure": deepcopy(document),
        "encounter": created,
    }


def _link_response(
    state: EngineState,
    document: Mapping[str, Any],
    version: str,
    member: Mapping[str, Any],
) -> dict[str, Any]:
    """A retried link, answered from what the document already recorded.

    The fight's creation payload is rebuilt rather than stored: it is the same
    answer ``encounter.create`` gives its own retry, and keeping a copy of every
    linked fight's initial state inside the adventure would make this document
    grow by a whole encounter per call.
    """
    encounter_id = str(member["encounter_id"])
    return {
        "adventure_id": str(document["id"]),
        "encounter_id": encounter_id,
        "index": int(member["index"]),
        "carried": [str(name) for name in member.get("carried", [])],
        "version": version,
        "adventure": deepcopy(dict(document)),
        "encounter": encounters.creation_response(
            state, encounter_id, sessions.session_for(state, encounter_id)
        ),
    }


def _creation_record(
    state: EngineState, encounter_id: str
) -> Mapping[str, Any]:
    """The record an encounter was *started* under, read off its journal.

    Not a live session, and for :func:`compose_replay`'s reason: what a chapter
    was created on is a frozen fact, and recovering a whole fight to read one
    field off it would replay every action the journal holds. The previous
    chapter is usually finalized by the time the next is linked, so its session
    may not even be in memory.
    """
    try:
        records, _warning = journal_service.read(
            encounter_id, root=sessions.encounters_dir_of(state)
        )
    except journal_service.JournalError as error:
        raise RequestError(
            f"cannot read encounter {encounter_id!r}: {error}"
        ) from error
    if not records or records[0].get("kind") != "creation":
        raise RequestError(
            f"encounter journal {encounter_id!r} does not begin with a creation record"
        )
    return records[0]


def _carried_map_id(
    state: EngineState,
    adventure_id: str,
    members: Sequence[Mapping[str, Any]],
    map_spec: Mapping[str, Any] | None,
    map_id: str | None,
) -> str:
    """The saved map the previous chapter was on, or a refusal that says which.

    The three map keys a creation record carries are **not** interchangeable and
    the refusals here are the reason it matters. ``map_kind`` is the
    discriminator; ``map_source`` is ``None`` for an inline map *and* for no map
    at all; and ``map_source["map_id"]`` means something only when the kind is
    ``loaded``. So a chapter given its map inline **has** a map and no id — a
    different complaint, with a different remedy, from a chapter that was never
    on one, and one message for both would send the caller to the wrong fix.
    """
    if map_spec is not None or map_id is not None:
        named = "'map'" if map_spec is not None else "'map_id'"
        raise RequestError(
            f"carry_map cannot be given with {named}: a link names the ground it "
            f"is carrying or the ground it is stating, never both"
        )
    if not members:
        raise RequestError(
            f"adventure {adventure_id!r} has no encounter to carry a map from yet; "
            f"give 'map_id' or 'map' for the first one"
        )
    previous = str(members[-1]["encounter_id"])
    created = _creation_record(state, previous)
    kind = created.get("map_kind")
    if kind == "inline":
        raise RequestError(
            f"cannot carry the map of encounter {previous!r}: it was given its "
            f"map inline, so it has no id to carry; save that map and name it, "
            f"or send the document again as 'map'"
        )
    source = created.get("map_source")
    if kind != "loaded" or not isinstance(source, Mapping):
        raise RequestError(
            f"cannot carry the map of encounter {previous!r}: it was not on a map"
        )
    carried = source.get("map_id")
    if not isinstance(carried, str) or not carried:
        raise RequestError(
            f"cannot carry the map of encounter {previous!r}: its record names no map id"
        )
    return carried


def _carried_specs(
    state: EngineState,
    adventure_id: str,
    members: Sequence[Mapping[str, Any]],
    carry: Sequence[str] | None,
    recovery: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]] | None]:
    """The previous encounter's cast, as creation specs for the next one."""
    if not members:
        if carry:
            raise RequestError(
                f"adventure {adventure_id!r} has no encounter to carry from yet; "
                f"give 'combatants' for the first one"
            )
        if recovery is not None:
            raise RequestError(
                f"adventure {adventure_id!r} has no encounter to recover from yet"
            )
        return [], None
    previous = str(members[-1]["encounter_id"])
    if carry is not None and not carry:
        # An explicit empty carry means the previous fight is not consulted at
        # all. Worth the branch rather than falling through: ``session_for``
        # recovers a fight by replaying every recorded action, which is a lot of
        # work to do to a fight nobody is bringing anybody out of.
        return [], _checked_recovery(recovery, set())
    session = sessions.session_for(state, previous)
    captured = {str(entry["name"]): entry for entry in session.normalized_combatants}
    live = {
        str(entry["name"]): entry for entry in session.encounter.state()["combatants"]
    }
    names = (
        [str(name) for name in carry]
        if carry is not None
        else [str(entry["name"]) for entry in session.normalized_combatants]
    )
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise RequestError(f"carry names {name!r} twice")
        seen.add(name)
        if name not in captured or name not in live:
            known = ", ".join(sorted(captured)) or "nobody"
            raise RequestError(
                f"cannot carry {name!r}: encounter {previous!r} had {known}"
            )
    deltas = _checked_recovery(recovery, seen)
    return (
        [
            carry_forward(
                captured[name], {**live[name], **(deltas or {}).get(name, {})}
            )
            for name in names
        ],
        deltas,
    )


def _checked_recovery_note(
    recovery: Mapping[str, Any] | None, recovery_note: str | None
) -> str | None:
    """One optional caller label, kept as replay prose and never interpreted."""
    if recovery_note is None:
        return None
    if recovery is None:
        raise RequestError("recovery_note requires recovery")
    if not isinstance(recovery_note, str):
        raise RequestError(f"recovery_note must be a string, got {recovery_note!r}")
    checked = recovery_note.strip()
    if not checked:
        raise RequestError("recovery_note must not be blank")
    if len(checked) > RECOVERY_NOTE_MAX:
        raise RequestError(
            f"recovery_note must be at most {RECOVERY_NOTE_MAX} characters"
        )
    return checked


def _checked_recovery(
    recovery: Mapping[str, Any] | None, carried: set[str]
) -> dict[str, dict[str, Any]] | None:
    """The caller's deltas, refused key by key rather than partly applied.

    ``bool(...)``-style leniency is the wrong tool here for the reason
    ``parse_carried_flag`` is: a recovery naming ``max_hp`` looks like it healed
    somebody and would change nothing at all, and a recovery naming a combatant
    who is not coming would silently do nothing to anybody.
    """
    if recovery is None:
        return None
    if not isinstance(recovery, Mapping):
        raise RequestError(
            f"recovery must be an object of combatant names to changes, got {recovery!r}"
        )
    checked: dict[str, dict[str, Any]] = {}
    for name in sorted(recovery):
        if name not in carried:
            coming = ", ".join(sorted(carried)) or "nobody"
            raise RequestError(
                f"cannot recover {name!r}: it is not being carried; carrying: {coming}"
            )
        delta = recovery[name]
        if not isinstance(delta, Mapping):
            raise RequestError(f"recovery for {name!r} must be an object, got {delta!r}")
        for key in sorted(set(delta) - CARRIED_STATE_KEYS):
            raise RequestError(
                f"unknown recovery key {key!r} for {name!r}. Valid keys: "
                f"{', '.join(sorted(CARRIED_STATE_KEYS))}"
            )
        checked[name] = deepcopy(dict(delta))
    return checked
