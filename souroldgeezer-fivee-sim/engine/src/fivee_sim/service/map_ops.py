"""Operations over a map *document*, and the replay bundles fights export to.

What unites these is their subject. A map here is a document — generated,
edited, rendered, queried, exported — not a fight; the only place a fight
enters is as an overlay on a render or as the encounter a replay was made from.

**A map is a file, addressed by id.** There used to be a dictionary of loaded
map sessions in front of the files, with generation counters to say how far a
copy had drifted. It is gone: an id is the ``slugify`` of a filename under the
maps directory, every operation reads the file it names, and the only version
anything compares against is the file's own canonical hash. That is what makes
two servers on one host agree about a map instead of each holding a private
copy it believes is current. The operations that only need a *document* — a
render, a query, a UVTT export — accept one inline as well, which is what keeps
generate -> look -> tweak -> save possible without anything being loaded.

Two policies live here rather than in the adapter because both are about the
*result*, not the transport. A map small enough renders inline and a larger one
returns ``render: null`` with instructions; a replay bundle small enough comes
back inline and a larger one is written to disk and answered with its path.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from importlib import resources
from pathlib import Path
from typing import Any

from .. import __version__
from ..kernel.grid import Square, TerrainTable, UnknownTerrain, as_point, to_square
from ..map_document import GROUND_LEVEL, MapDocument, MapLevel, as_payload
from ..map_document import serialize as serialize_map
from . import maps as map_service
from . import replay as replay_service
from . import sessions, specs
from . import uvtt as uvtt_service
from .common import sha256_of, slugify
from .errors import MapEditError, RequestError, StaleWriteError
from .sessions import EngineState

__all__ = [
    "INLINE_BUNDLE_BYTES",
    "INLINE_RENDER_CELLS",
    "document_of",
    "edit",
    "edit_render",
    "encounter_tokens",
    "generate",
    "get_map",
    "map_list",
    "map_summary",
    "query_map",
    "render",
    "replay_export",
    "replay_validate",
    "save_map",
    "storey_summary",
    "terrain_of",
    "uvtt_export",
]

#: A map at or under this many squares renders inline in tool results; a larger
#: one returns ``render: null`` and a pointer at map_render's viewports.
INLINE_RENDER_CELLS = 4000
#: A serialized replay bundle at or under this many bytes is returned inline;
#: a larger one is written to disk and answered with its path — the same
#: result-size rule the map tools follow for oversized documents.
INLINE_BUNDLE_BYTES = 64 * 1024
TOKEN_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# --- summaries -------------------------------------------------------------
def storey_summary(level: MapLevel, legend: Mapping[str, str]) -> dict[str, Any]:
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


def map_summary(document: MapDocument) -> dict[str, Any]:
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
    storeys = [storey_summary(level, document.legend) for level in levels]
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


# --- renders ---------------------------------------------------------------
def _on_document(document: MapDocument, square: Square) -> bool:
    return 0 <= square[0] < document.grid.width and 0 <= square[1] < document.grid.height


def encounter_tokens(
    state: EngineState, document: MapDocument, encounter_id: str
) -> tuple[dict[Square, str], dict[str, str]]:
    """Combatant overlay marks for a render: letters by initiative, ``x`` downed.

    Only combatants standing inside the document's bounds appear. Downed
    bodies are placed first, so a conscious combatant sharing a square wins
    the cell, matching the rule that a downed body blocks nothing.
    """
    fight = sessions.session_for(state, encounter_id).encounter
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
        if index < len(TOKEN_LETTERS):
            letter = TOKEN_LETTERS[index]
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


def edit_render(before: MapDocument, after: MapDocument) -> dict[str, Any]:
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
    if width * height <= INLINE_RENDER_CELLS:
        return map_service.render_ascii(after, level=level)
    changed = touched.get(level) or []
    if changed:
        xs = [square[0] for square in changed]
        ys = [square[1] for square in changed]
    else:
        xs, ys = [0, width - 1], [0, height - 1]
    x0, y0 = min(xs), min(ys)
    box_w, box_h = max(xs) - x0 + 1, max(ys) - y0 + 1
    downsample = 1
    while -(-box_w // downsample) * -(-box_h // downsample) > map_service.RENDER_BUDGET:
        downsample += 1
    return map_service.render_ascii(
        after, x=x0, y=y0, width=box_w, height=box_h, downsample=downsample, level=level
    )


# --- resolving what a call is about ----------------------------------------
def terrain_of(state: EngineState) -> TerrainTable:
    return sessions.active_registry(state).terrain_effects


def document_of(
    state: EngineState, map_id: str | None, document: dict[str, Any] | None
) -> tuple[MapDocument, str | None]:
    """The document one call is about: a saved map's, or one given inline.

    Exactly one, and the refusal names both — an operation over "the map" has
    to know which map, and silently preferring one of two given subjects is how
    a caller ends up looking at a document it did not send.
    """
    if (map_id is None) == (document is None):
        raise RequestError("give exactly one of 'map_id' (a saved map) or 'document' (inline)")
    if map_id is not None:
        loaded, _path = map_service.load_by_id(
            map_id, sessions.maps_dir_of(state), terrain=terrain_of(state)
        )
        return loaded, map_id
    assert document is not None
    parsed, _warnings = map_service.parse_payload(
        document, source="inline", terrain=terrain_of(state)
    )
    return parsed, None


def map_list(state: EngineState) -> dict[str, Any]:
    """Every saved map under this adapter's maps directory, keyed by id."""
    found = map_service.index(sessions.maps_dir_of(state))
    return {"maps": [found[map_id] for map_id in sorted(found)]}


def get_map(state: EngineState, map_id: str) -> dict[str, Any]:
    """One saved map: its canonical payload, and the hash that identifies it."""
    document, path = map_service.load_by_id(
        map_id, sessions.maps_dir_of(state), terrain=terrain_of(state)
    )
    return {
        "map_id": map_id,
        "path": str(path),
        "sha256": sha256_of(serialize_map(document)),
        "document": as_payload(document),
    }


def save_map(
    state: EngineState,
    map_id: str,
    document: dict[str, Any],
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Write a document under ``map_id``, refusing a write from a stale read.

    ``expected_sha256`` is the version the caller read: ``"*"`` writes
    regardless, a hash is compared *under the write lock* rather than before it,
    and ``None`` requires the id to be free — a caller who did not read cannot
    claim to know what it is replacing.
    """
    directory = sessions.maps_dir_of(state)
    target = map_service.path_for_id(map_id, directory)
    parsed, warnings = map_service.parse_payload(
        document, source=map_id, terrain=terrain_of(state)
    )
    expected = None if expected_sha256 == "*" else expected_sha256
    try:
        saved = map_service.save_file(
            parsed,
            target,
            overwrite=expected_sha256 is not None,
            expected_sha256=expected,
            terrain=terrain_of(state),
        )
    except OSError as error:
        raise RequestError(f"cannot write {target}: {error}") from error
    except StaleWriteError:
        # A ValueError like the rest, and the one this must not flatten: the
        # caller's remedy is to re-read, which "bad request" does not say.
        raise
    except ValueError as error:
        if expected is None and target.exists():
            # ``overwrite`` names an argument neither adapter's caller has:
            # what they have is the version they read, so say that instead.
            raise RequestError(
                f"map {map_id!r} already exists; supply the version you read "
                f"to replace it, or '*' to take it over"
            ) from error
        raise RequestError(str(error)) from error
    return {
        "saved": True,
        "map_id": map_id,
        "created": expected is None,
        "path": str(saved["path"]),
        "bytes": saved["bytes"],
        "sha256": saved["sha256"],
        "name": parsed.name,
        "summary": map_summary(parsed),
        "warnings": [warning.as_dict() for warning in warnings],
        "provenance": as_payload(parsed)["provenance"],
    }


# --- the map operations ----------------------------------------------------
def generate(
    state: EngineState,
    kind: str,
    params: dict[str, Any] | None = None,
    seed: int | None = None,
    name: str | None = None,
    save_as: str | None = None,
) -> dict[str, Any]:
    """Generate a map under a seed; ``save_as`` also writes it under that id.

    The document comes back either way. Generation is cheap and reproducible
    from its seed, so a caller is free to look before keeping — but keeping it
    is one call rather than two, because the alternative was a session holding
    the result in the meantime, which is the thing that no longer exists.
    """
    used = specs.checked_seed(seed)
    try:
        document = map_service.generate(kind, params, used, name=name)
    except ValueError as error:
        raise RequestError(str(error)) from error
    result: dict[str, Any] = {
        "map_id": None,
        "seed": used,
        "kind": kind,
        "name": document.name,
        "params": dict(document.provenance.params),
        "summary": map_summary(document),
        "provenance": as_payload(document)["provenance"],
        "document": as_payload(document),
    }
    if document.grid.width * document.grid.height <= INLINE_RENDER_CELLS:
        result["render"] = map_service.render_ascii(document)
    else:
        result["render"] = None
        result["note"] = (
            "the map is too large to render inline; render it with a viewport "
            "(x, y, width, height) or a downsample factor"
        )
    if save_as is not None:
        saved = save_map(state, save_as, as_payload(document), expected_sha256=None)
        result["map_id"] = save_as
        result["saved"] = saved
    return result


def render(
    state: EngineState,
    map_id: str | None = None,
    document: dict[str, Any] | None = None,
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
    subject, resolved_id = document_of(state, map_id, document)
    tokens: dict[Square, str] = {}
    letters: dict[str, str] = {}
    open_features: list[str] | None = None
    if encounter_id is not None:
        tokens, letters = encounter_tokens(state, subject, encounter_id)
        # The fight's live fixture states. A mapless fight has none, and a fight
        # on some other map contributes names this document simply does not
        # have — the same leniency the token overlay already takes with a
        # position that lands off the map.
        map_state = sessions.session_for(state, encounter_id).encounter.map_state
        if map_state is not None:
            open_features = sorted(map_state.open_features)
    try:
        rendered = map_service.render_ascii(
            subject,
            x=x, y=y, width=width, height=height,
            downsample=downsample, show_features=show_features,
            show_elevation=show_elevation, level=level,
            tokens=tokens or None, open=open_features,
        )
    except ValueError as error:
        raise RequestError(str(error)) from error
    result: dict[str, Any] = {
        "map_id": resolved_id,
        "sha256": sha256_of(serialize_map(subject)),
        **rendered,
    }
    if encounter_id is not None:
        result["tokens"] = letters
    return result


def edit(
    state: EngineState,
    map_id: str,
    operations: list[dict[str, Any]],
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Apply edits to a saved map and write the result: all of them, or none.

    A read-modify-write, so the version read is the precondition the write
    carries — without it two editors of one file each acknowledge an edit and
    the slower one silently discards the faster one's. The caller may name the
    version it read; by default it is the one this call just read, which closes
    the window between the two rather than leaving it open by default.
    """
    directory = sessions.maps_dir_of(state)
    path = map_service.resolve_id(map_id, directory)
    before, _warnings = map_service.load_file(path, terrain=terrain_of(state))
    base = expected_sha256 or sha256_of(serialize_map(before))
    try:
        after = map_service.apply_edits(before, operations, terrain=terrain_of(state))
    except MapEditError:
        raise
    except ValueError as error:
        raise RequestError(str(error)) from error
    saved = map_service.save_file(
        after,
        path,
        overwrite=True,
        expected_sha256=None if base == "*" else base,
        terrain=terrain_of(state),
    )
    return {
        "saved": True,
        "applied": len(operations),
        "map_id": map_id,
        "sha256": saved["sha256"],
        "edited": after.provenance.edited,
        "summary": map_summary(after),
        "render": edit_render(before, after),
        "document": as_payload(after),
    }


def query_map(
    state: EngineState,
    map_id: str | None = None,
    document: dict[str, Any] | None = None,
    query: str = "",
    frm: list[int] | None = None,
    to: list[int] | None = None,
    level: int = 0,
) -> dict[str, Any]:
    subject, resolved_id = document_of(state, map_id, document)

    def _square_arg(value: list[int] | None, what: str) -> Square:
        if not (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(part, int) and not isinstance(part, bool) for part in value)
        ):
            raise RequestError(f"{what} must be an [x, y] pair of squares")
        return (value[0], value[1])

    origin = _square_arg(frm, "frm")
    target = _square_arg(to, "to")
    try:
        answer = map_service.query(
            subject, query, origin, target,
            terrain=terrain_of(state), level=level,
        )
    except (UnknownTerrain, ValueError) as error:
        raise RequestError(str(error)) from error
    return {"map_id": resolved_id, **answer}


def uvtt_export(
    state: EngineState,
    map_id: str | None = None,
    document: dict[str, Any] | None = None,
    path: str | None = None,
    pixels_per_grid: int = 32,
    include_image: bool = True,
    level: int = 0,
    open_features: list[str] | None = None,
) -> dict[str, Any]:
    subject, resolved_id = document_of(state, map_id, document)
    try:
        payload = uvtt_service.to_uvtt(
            subject,
            terrain=terrain_of(state),
            pixels_per_grid=pixels_per_grid,
            include_image=include_image,
            level=level,
            open=open_features,
        )
    except (UnknownTerrain, ValueError) as error:
        raise RequestError(str(error)) from error
    target = (
        Path(path).expanduser()
        if path is not None
        else map_service.maps_root() / "uvtt" / f"{slugify(subject.name)}.uvtt"
    )
    text = json.dumps(payload) + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as error:
        raise RequestError(f"cannot write {target}: {error}") from error
    return {
        "path": str(target),
        "bytes": len(text.encode("utf-8")),
        "map_id": resolved_id,
        "resolution": payload["resolution"],
        "wall_polylines": len(payload["line_of_sight"]),
        "portals": len(payload["portals"]),
        "image": include_image,
    }


# --- replays ---------------------------------------------------------------
def replay_validate(bundle: dict[str, Any]) -> dict[str, Any]:
    """Grade a bundle, choosing the validator by what the document says it is.

    Two formats reach this one operation: a fight's replay and an adventure's
    composed replay, which nests fights as chapters. They share no required
    field, so the ``format`` discriminator picks the validator rather than one
    validator growing a second shape — and a caller holding a file it did not
    compose does not have to know which it has before asking.

    An unrecognised ``format`` falls through to the replay validator, which
    names it as the first diagnostic. That is the right answer for a document
    that is neither: the reply says what it should have been.
    """
    if (
        isinstance(bundle, Mapping)
        and bundle.get("format") == replay_service.ADVENTURE_FORMAT
    ):
        diagnostics = replay_service.validate_adventure_replay(bundle)
    else:
        diagnostics = replay_service.validate_replay(bundle)
    return {
        "valid": not diagnostics,
        "error_count": len(diagnostics),
        "diagnostics": diagnostics,
    }


def replay_export(
    state: EngineState,
    encounter_id: str,
    path: str | None = None,
    embed: bool = False,
    format_version: int = replay_service.LATEST_FORMAT_VERSION,
    viewer_link: Callable[[Path], str | None] | None = None,
) -> dict[str, Any]:
    """Serialise a fight as a replay bundle, inline or on disk.

    ``viewer_link`` is how a *running* viewer gets named without this layer
    knowing what a URL is: the adapter passes a callable that answers a link
    for a written file, or ``None`` for one it cannot play. Omitting it exports
    the bundle and says nothing about where it might be watched.
    """
    session = sessions.session_for(state, encounter_id)
    if format_version == 1:
        name = (
            str(session.map_payload["name"])
            if session.map_payload is not None
            else encounter_id
        )
        bundle = replay_service.replay_bundle(
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
        latest_state["map_source"] = sessions.map_source_of(state, session)
        initial_state = deepcopy(session.initial_state)
        initial_state["map_source"] = sessions.map_source_of(state, session)
        checkpoints = []
        for index, captured_state in enumerate(session.state_history):
            checkpoint_state = deepcopy(captured_state)
            checkpoint_state["map_source"] = sessions.map_source_of(state, session)
            checkpoints.append(
                {
                    "index": index,
                    "timestamp": session.checkpoint_timestamps[index],
                    "event_count": session.checkpoint_event_counts[index],
                    "state_hash": replay_service.canonical_sha256(checkpoint_state),
                    "state": checkpoint_state,
                }
            )
        bundle = replay_service.replay_bundle_v2(
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
        raise RequestError(f"format_version must be 1 or 2, got {format_version}")
    serialized = replay_service.serialize_bundle(bundle)
    slug = slugify(name)
    result: dict[str, Any] = {
        "encounter_id": encounter_id,
        "seed": session.seed,
        "format": replay_service.FORMAT,
        "events": len(session.encounter.log),
        "sha256": replay_service.sha256_bytes(serialized.encode("utf-8")),
    }

    if embed:
        static = resources.files("fivee_sim.web") / "static"
        viewer = (static / "viewer.html").read_text(encoding="utf-8")
        renderer = (static / "renderer.js").read_text(encoding="utf-8")
        html = replay_service.embed_in_viewer(
            viewer, serialized, renderer_js=renderer
        )
        target = (
            Path(path).expanduser()
            if path is not None
            else replay_service.replays_root() / f"{slug}-{session.seed}.html"
        )
        try:
            replay_service.atomic_write_text(target, html)
        except OSError as error:
            raise RequestError(f"cannot write {target}: {error}") from error
        return {
            **result,
            "path": str(target),
            "bytes": len(html.encode("utf-8")),
            "sha256": replay_service.sha256_bytes(html.encode("utf-8")),
        }

    size = len(serialized.encode("utf-8"))
    if path is None and size <= INLINE_BUNDLE_BYTES:
        return {**result, "bundle": bundle, "bytes": size}
    target = (
        Path(path).expanduser()
        if path is not None
        else replay_service.replays_root() / f"{slug}-{session.seed}.json"
    )
    try:
        replay_service.atomic_write_text(target, serialized)
    except OSError as error:
        raise RequestError(f"cannot write {target}: {error}") from error
    written = {
        **result,
        "path": str(target),
        "bytes": size,
        "sha256": replay_service.sha256_bytes(serialized.encode("utf-8")),
    }
    # Only the bundle branch: an embedded .html is opened directly and is not
    # in the served replay listing, which reads bundles alone.
    viewer_url = viewer_link(target) if viewer_link is not None else None
    return written if viewer_url is None else {**written, "viewer_url": viewer_url}
