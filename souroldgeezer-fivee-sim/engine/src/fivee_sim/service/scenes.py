"""A scene: the fight a table is about to have, saved as a file.

A scene is a stored ``encounter.create`` body — ``combatants``, ``seed``,
``movement_rule``, and either an inline ``map`` or a saved ``map_id`` — with a
``name`` to list it by and the ``content_paths`` it wants loaded first. Nothing
more. Storage is :mod:`~fivee_sim.service.durable`'s, exactly as a map's is: a
sha256 of the file is its version, ``guarded_write`` refuses a writer who read
an older one, and ``atomic_write`` publishes by rename so a reader never sees a
prefix.

**There is no ``scene.play``.** Play posts the stored body to
``encounter.create``, so exactly one code path starts a fight. A second one here
would be a quieter way into the same place, and the quiet way is the one that
drifts.

**The envelope is validated; the specs inside it are not.** Whether a combatant
specification is legal belongs to ``encounter.create``, which refuses it at Play
time in its own words, and which movement rules exist belongs to
``kernel.grid``. This layer checks the shape around them — required keys, a seed
that is a whole number, never both a ``map`` and a ``map_id`` — and stops. Two
owners of "what a valid combatant spec is" would be one rule with a copy, and
the copy drifts the first time a spec field is added.

The cost is deliberate and worth naming: **a scene can be saved that will not
start.** An editor buffer is a draft, and refusing to save a draft because the
fight it describes is not ready yet is the worse failure.

**It imports neither ``model`` nor ``kernel``, and that is enforced.** A scene
is a request body, not a domain object. The one check that would need another
layer — does this ``map_id`` name a saved map — takes the known ids as an
argument rather than reaching for the maps directory, because reaching for it
would pull in the map document, the grid, and the kernel behind them.
``tests/test_layering.py`` holds the boundary, which is also why the imports
below name their modules — ``from .durable import guarded_write`` rather than
``from . import durable``, since a bare package import reads as *this module
may reach anywhere in* ``service/`` and is scored as exactly that.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

from ..paths import SCENES_ENV, SCENES_SUBDIR, scenes_root
from ..validation import Diagnostic, Severity
from .common import ID_PATTERN, discover_json_files, sha256_of, slugify
from .durable import guarded_write
from .errors import NotFoundError, RequestError, StaleWriteError

__all__ = [
    "ENCOUNTER_KEYS",
    "ID_PATTERN",
    "SCENES_ENV",
    "SCENES_SUBDIR",
    "SCENE_KEYS",
    "diagnose",
    "index",
    "list_scenes",
    "load",
    "path_for_id",
    "render",
    "resolve_id",
    "save",
    "scenes_root",
    "validate",
]

#: The keys that *are* an ``encounter.create`` body, in the order that operation
#: declares them. ``tests/test_scene_service.py`` holds this against the route
#: table's own schema rather than restating it, so a key added to
#: ``encounter.create`` and not to a scene fails there instead of being silently
#: dropped from every fight a scene starts.
ENCOUNTER_KEYS: tuple[str, ...] = (
    "combatants", "seed", "mode", "movement_rule", "map", "map_id",
)

#: Everything a stored scene may carry: the encounter body, a label to list it
#: by, and the packs it wants loaded before it runs. ``name`` and
#: ``content_paths`` are deliberately *not* posted to ``encounter.create`` —
#: a label is not a fight, and content is configured, not created.
SCENE_KEYS: tuple[str, ...] = ("name", *ENCOUNTER_KEYS, "content_paths")

#: The version a file that is not there reports. A sentinel rather than ``None``
#: because :func:`~fivee_sim.service.durable.guarded_write` reads ``None`` as
#: *no precondition at all*, and "this must not exist yet" is a precondition —
#: it is what makes a create safe against a second process.
_ABSENT = ""

_SECTION = "scene"


def render(document: Mapping[str, Any]) -> str:
    """The scene's canonical text — what its sha256 version is the hash of.

    Sorted and indented, so two writers who composed the same scene in a
    different key order publish the same bytes and a saved scene diffs cleanly.
    """
    return json.dumps(dict(document), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# --- the envelope ------------------------------------------------------------
def _problem(field: str, problem: str, source: str, severity: Severity) -> Diagnostic:
    return Diagnostic(
        source=source, section=_SECTION, field=field, problem=problem, severity=severity
    )


def _error(field: str, problem: str, source: str) -> Diagnostic:
    return _problem(field, problem, source, Severity.ERROR)


def _is_whole(value: Any) -> bool:
    """A whole number, and never a ``bool`` — which is an ``int`` in Python."""
    return isinstance(value, int) and not isinstance(value, bool)


def diagnose(
    document: Any,
    *,
    source: str = "scene",
    map_ids: Collection[str] | None = None,
) -> list[Diagnostic]:
    """Every problem with this envelope, collected rather than the first one.

    ``map_ids`` is *the ids the caller can see*, and ``None`` means the caller
    has no map index — not that there are no maps. A validator that resolved
    ids itself would need the maps directory and everything under it; the
    adapter passes its launch's index instead, and a call that passes nothing
    simply does not make that check.

    What is checked is the shape of the request body. What a combatant spec
    means, and which movement rules exist, belong to the operation that
    resolves them.
    """
    if not isinstance(document, Mapping):
        return [_error("", "a scene must be an object with keys from: "
                       f"{', '.join(SCENE_KEYS)}", source)]

    found: list[Diagnostic] = []
    unknown = sorted(set(document) - set(SCENE_KEYS))
    if unknown:
        found.append(_error(
            "",
            f"unknown key(s): {', '.join(repr(key) for key in unknown)}. "
            f"Valid keys: {', '.join(SCENE_KEYS)}",
            source,
        ))

    if "combatants" not in document:
        found.append(_error(
            "combatants", "'combatants' is required: a scene is a roster", source
        ))
    elif not isinstance(document["combatants"], list):
        found.append(_error("combatants", "'combatants' must be a list of specs", source))
    else:
        # Shape, not meaning: the contract declares this array's items as
        # objects, so a bare name is a malformed body. Whether the object
        # *describes a creature* is ``encounter.create``'s to answer.
        for position, entry in enumerate(document["combatants"]):
            if not isinstance(entry, Mapping):
                found.append(_error(
                    "combatants",
                    f"combatant #{position} must be an object; what it may hold is "
                    f"encounter.create's to say",
                    source,
                ))

    name = document.get("name")
    if "name" in document and (not isinstance(name, str) or not name.strip()):
        found.append(_error("name", f"'name' must be non-empty text, got {name!r}", source))

    seed = document.get("seed")
    if "seed" in document and seed is not None and not _is_whole(seed):
        found.append(_error(
            "seed", f"'seed' must be a whole number or null, got {seed!r}", source
        ))

    rule = document.get("movement_rule")
    if "movement_rule" in document and not isinstance(rule, str):
        found.append(_error(
            "movement_rule",
            f"'movement_rule' must be text; which rules exist is the fight's to "
            f"refuse, got {rule!r}",
            source,
        ))

    if "map" in document and document["map"] is not None:
        if not isinstance(document["map"], Mapping):
            # An object, and no narrower: ``encounter.create`` takes two inline
            # shapes here — a ``fivee-sim-map`` document, which is what the
            # editor's Play button posts, or a battle-map spec — and which of
            # them this is belongs to the operation that resolves it, exactly
            # as a combatant spec's meaning does.
            found.append(_error(
                "map",
                "'map' must be an object: a fivee-sim-map document or a "
                "battle-map spec",
                source,
            ))
        if document.get("map_id") is not None:
            found.append(_error(
                "map_id",
                "give 'map' (an inline map) or 'map_id' (a saved map), not both",
                source,
            ))

    map_id = document.get("map_id")
    if map_id is not None:
        if not isinstance(map_id, str) or not map_id.strip():
            found.append(_error(
                "map_id", f"'map_id' must name a saved map, got {map_id!r}", source
            ))
        elif map_ids is not None and map_id not in map_ids:
            known = ", ".join(sorted(map_ids)) or "none"
            found.append(_error(
                "map_id", f"no saved map {map_id!r}; maps here: {known}", source
            ))

    paths = document.get("content_paths")
    if paths is not None:
        if not isinstance(paths, list):
            found.append(_error(
                "content_paths", "'content_paths' must be a list of pack paths", source
            ))
        elif not all(isinstance(entry, str) for entry in paths):
            found.append(_error(
                "content_paths", "every entry in 'content_paths' must be text", source
            ))

    if document.get("seed") is None:
        found.append(_problem(
            "seed",
            "this scene names no seed, so every fight it starts rolls a different "
            "one; save the seed to replay the same fight",
            source,
            Severity.WARNING,
        ))
    return found


def validate(
    document: Any,
    *,
    source: str = "request",
    map_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    """Report a scene's errors and warnings without storing it.

    The shape ``map.validate`` answers in, because it answers the same question
    about a different document.
    """
    found = diagnose(document, source=source, map_ids=map_ids)
    errors = [one.as_dict() for one in found if one.severity is Severity.ERROR]
    warnings = [one.as_dict() for one in found if one.severity is Severity.WARNING]
    return {"ok": not errors, "errors": errors, "warnings": warnings}


# --- files -------------------------------------------------------------------
def _root(root: str | Path | None) -> Path:
    """The directory a call names, or the configured one, resolved *per call*.

    Never captured at import: the variable is what a host sets, and a module
    level default would make every launch in a process share whichever
    directory happened to be configured when this module was first imported.
    """
    return Path(root).expanduser() if root is not None else scenes_root()


def path_for_id(scene_id: str, root: str | Path | None = None) -> Path:
    """Where a scene id lives under ``root``, whether or not it exists yet.

    An id outside the grammar is *not found* rather than malformed, the rule
    ``maps.path_for_id`` follows and for the same reason: it cannot name a file
    we wrote, so there is nothing to diagnose beyond its absence. Containment is
    checked rather than assumed, so a symlink pointing out of the scenes
    directory is an unknown scene rather than a file we follow.
    """
    if ID_PATTERN.fullmatch(scene_id) is None:
        raise NotFoundError(f"no scene {scene_id!r}")
    directory = _root(root)
    target = directory / f"{scene_id}.json"
    if target.exists() and not target.resolve().is_relative_to(directory.resolve()):
        raise NotFoundError(f"no scene {scene_id!r} under {directory}")
    return target


def _entry(scene_id: str, path: Path, document: Mapping[str, Any]) -> dict[str, Any]:
    """One listing row: enough to choose a scene, never the whole roster."""
    return {
        "id": scene_id,
        "path": str(path),
        "name": document.get("name"),
        "combatants": len(document.get("combatants") or ()),
        "seed": document.get("seed"),
        "map_id": document.get("map_id"),
        "inline_map": document.get("map") is not None,
    }


def _scene_in(text: str) -> dict[str, Any] | None:
    """The scene this text holds, or ``None`` if it is not one.

    A listing shows what is usable, so a file that is not a scene is skipped
    rather than reported — :func:`load` is where a named id's absence gets said
    out loud. "Is a scene" is the envelope's own answer: a JSON object whose
    keys are scene keys and whose required one is there.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if set(payload) - set(SCENE_KEYS) or "combatants" not in payload:
        return None
    return payload


def _readable(path: Path) -> dict[str, Any] | None:
    """The scene in this file, or ``None`` if it cannot be read as one."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _scene_in(text)


def _files(root: str | Path | None) -> Sequence[Path]:
    """Every candidate file under the scenes directory, containment applied.

    Shared with the map and replay listings rather than globbed here, because
    the rule it carries is a security rule: a directory the caller configured
    does not authorise whatever a symlink inside it points at, and two copies of
    that check are two chances for one of them to drift wider.
    """
    directory = _root(root)
    if not directory.is_dir():
        return ()
    return discover_json_files([directory])


def index(root: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Every scene under ``root``, keyed by id.

    An id is the ``slugify`` of the file's stem — the same name a save writes —
    so a URL naming a scene and a tool naming one agree. Two files slugifying to
    one id collide; the first in path order claims it, the first-wins rule
    everywhere else this engine merges.
    """
    found: dict[str, dict[str, Any]] = {}
    for path in _files(root):
        document = _readable(path)
        if document is None:
            continue
        scene_id = slugify(path.stem)
        if scene_id in found:
            continue
        found[scene_id] = _entry(scene_id, path, document)
    return found


def scoped_index(
    roots: Sequence[tuple[str | Path, str]],
) -> dict[str, dict[str, Any]]:
    """Scenes merged by id in root order, with their selected storage scope."""
    found: dict[str, dict[str, Any]] = {}
    for root, scope in roots:
        for scene_id, entry in index(root).items():
            if scene_id in found:
                continue
            found[scene_id] = {**entry, "scope": scope}
    return found


def list_scenes(
    root: str | Path | None = None,
    *,
    scoped_roots: Sequence[tuple[str | Path, str]] | None = None,
) -> dict[str, Any]:
    """Every saved scene, in id order — the ``scene.list`` operation's body."""
    found = scoped_index(scoped_roots) if scoped_roots is not None else index(root)
    return {"scenes": [found[scene_id] for scene_id in sorted(found)]}


def resolve_id(
    scene_id: str,
    root: str | Path | None = None,
    *,
    scoped_roots: Sequence[tuple[str | Path, str]] | None = None,
) -> Path:
    """The file a scene id names, or a refusal that says what is there.

    Naming the alternatives is what makes it actionable: a bare "not found"
    leaves the caller guessing whether the id was wrong or the directory empty.
    """
    if ID_PATTERN.fullmatch(scene_id) is None:
        raise NotFoundError(f"no scene {scene_id!r}")
    found = scoped_index(scoped_roots) if scoped_roots is not None else index(root)
    entry = found.get(scene_id)
    if entry is None:
        known = ", ".join(sorted(found)) or "none"
        raise NotFoundError(f"no scene {scene_id!r}; scenes here: {known}")
    return Path(str(entry["path"]))


def load(
    scene_id: str,
    root: str | Path | None = None,
    *,
    scoped_roots: Sequence[tuple[str | Path, str]] | None = None,
) -> dict[str, Any]:
    """One saved scene, and the version a write against it must match.

    Reading is generous where writing is strict: what comes back is whatever
    was stored, envelope problems and all, because a draft that no longer
    validates is exactly the draft its author needs to open and fix.
    """
    path = resolve_id(scene_id, root, scoped_roots=scoped_roots)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:  # pragma: no cover - lost in a race
        raise RequestError(f"cannot read {path}: {error}") from error
    # One read, hashed and parsed: reading twice would let a writer land
    # between them and return a version the ETag does not describe.
    document = _scene_in(text)
    if document is None:  # pragma: no cover - lost in a race with a writer
        raise NotFoundError(f"no scene {scene_id!r}; {path} is not a scene")
    return {
        "scene_id": scene_id,
        "path": str(path),
        "sha256": sha256_of(text),
        "document": document,
    }


def _current_version(path: Path) -> str:
    """The version on disk, or :data:`_ABSENT` when there is no file yet.

    Hashed as bytes rather than re-rendered: every write here publishes
    :func:`render`'s canonical text, so the two agree, and hashing what is
    actually there means a caller's precondition is checked against the file
    rather than against our idea of it.
    """
    try:
        return sha256_of(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _ABSENT
    except (OSError, UnicodeDecodeError):
        return "unreadable"


def _refuse(scene_id: str, problems: Sequence[Diagnostic]) -> NoReturn:
    """Every problem at once, never the first: one trip round the loop."""
    listed = "; ".join(one.problem for one in problems)
    raise RequestError(f"scene {scene_id!r} cannot be saved: {listed}")


def save(
    scene_id: str,
    document: Any,
    root: str | Path | None = None,
    *,
    expected_sha256: str | None = None,
    baseline_roots: Sequence[str | Path] = (),
) -> dict[str, Any]:
    """Write a scene under ``scene_id``, refusing a write from a stale read.

    ``expected_sha256`` is the version the caller read: ``"*"`` writes
    regardless, a hash is compared *under the write lock* rather than before it,
    and ``None`` requires the id to be free — a caller who did not read cannot
    claim to know what it is replacing. That last case is a precondition too,
    which is why it is expressed as one rather than as a check-then-write: two
    processes creating the same id would both pass an ``exists()`` test.

    The envelope is validated first and an invalid one never reaches disk. Its
    *warnings* ride back with the result, as a map's do.
    """
    found = diagnose(document, source=scene_id)
    errors = [one for one in found if one.severity is Severity.ERROR]
    if errors:
        _refuse(scene_id, errors)
    target = path_for_id(scene_id, root)
    baseline: Path | None = None
    if not target.exists():
        for candidate_root in baseline_roots:
            candidate = path_for_id(scene_id, candidate_root)
            if candidate.is_file():
                baseline = candidate
                break
    text = render(document)
    if expected_sha256 is None:
        expected: str | None = _ABSENT
    elif expected_sha256 == "*":
        expected = None
    else:
        expected = expected_sha256
    try:
        guarded_write(
            target,
            lambda: text,
            expected=expected,
            # Read under the lock, never before it: computing this first is
            # exactly the race the precondition exists to close.
            current=lambda: _current_version(
                target if target.exists() or baseline is None else baseline
            ),
            subject=f"the saved scene {scene_id!r}",
        )
    except StaleWriteError:
        if expected_sha256 is None:
            raise RequestError(
                f"scene {scene_id!r} already exists; supply the version you read "
                f"to replace it, or '*' to take it over"
            ) from None
        raise
    except OSError as error:
        raise RequestError(f"cannot write {target}: {error}") from error
    return {
        "saved": True,
        "scene_id": scene_id,
        "path": str(target),
        "bytes": len(text.encode("utf-8")),
        "sha256": sha256_of(text),
        "name": document.get("name"),
        "combatants": len(document.get("combatants") or ()),
        "warnings": [
            one.as_dict() for one in found if one.severity is Severity.WARNING
        ],
    }
