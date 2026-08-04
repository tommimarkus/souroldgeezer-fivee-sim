"""Operations over a map *document*, and the replay bundles fights export to.

What unites these is their subject. A map here is a document — generated,
loaded, edited, rendered, queried, exported — not a fight; the session that
holds one is a handle on a document, and the only place a fight enters is as an
overlay on a render or as the encounter a replay was made from.

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
from ..kernel.grid import Square, UnknownTerrain, as_point, to_square
from ..map_document import GROUND_LEVEL, MapDocument, MapLevel, as_payload
from ..map_document import serialize as serialize_map
from . import maps as map_service
from . import replay as replay_service
from . import sessions, specs
from . import uvtt as uvtt_service
from .common import sha256_of, slugify
from .errors import RequestError
from .sessions import EngineState

__all__ = [
    "INLINE_BUNDLE_BYTES",
    "INLINE_RENDER_CELLS",
    "edit",
    "edit_render",
    "encounter_tokens",
    "generate",
    "load",
    "map_summary",
    "query_map",
    "render",
    "replay_export",
    "replay_validate",
    "save",
    "storey_summary",
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


# --- the map tools ---------------------------------------------------------
def generate(
    state: EngineState,
    kind: str,
    params: dict[str, Any] | None = None,
    seed: int | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    used = specs.checked_seed(seed)
    try:
        document = map_service.generate(kind, params, used, name=name)
    except ValueError as error:
        raise RequestError(str(error)) from error
    map_id = sessions.new_map_id(state)
    state.maps[map_id] = sessions.MapSession(document=document)
    result: dict[str, Any] = {
        "map_id": map_id,
        "seed": used,
        "kind": kind,
        "name": document.name,
        "params": dict(document.provenance.params),
        "summary": map_summary(document),
        "provenance": as_payload(document)["provenance"],
    }
    if document.grid.width * document.grid.height <= INLINE_RENDER_CELLS:
        result["render"] = map_service.render_ascii(document)
    else:
        result["render"] = None
        result["note"] = (
            "the map is too large to render inline; call map_render with a viewport "
            "(x, y, width, height) or a downsample factor"
        )
    return result


def load(
    state: EngineState,
    path: str | None = None,
    document: dict[str, Any] | None = None,
    replace: str | None = None,
) -> dict[str, Any]:
    if (path is None) == (document is None):
        raise RequestError("give exactly one of 'path' (a file) or 'document' (inline JSON)")
    terrain = sessions.active_registry(state).terrain_effects
    try:
        if path is not None:
            loaded, warnings = map_service.load_file(path, terrain=terrain)
        else:
            assert document is not None
            loaded, warnings = map_service.parse_payload(
                document, source="inline", terrain=terrain
            )
    except ValueError as error:
        raise RequestError(str(error)) from error
    # Only a file gives this session a version to guard against; an inline
    # document has no disk state to be stale relative to.
    on_disk = sha256_of(serialize_map(loaded)) if path is not None else ""
    if replace is not None:
        session = sessions.map_session_for(state, replace)
        session.document = loaded
        session.generation += 1
        session.path = path
        session.disk_sha256 = on_disk
        map_id = replace
    else:
        map_id = sessions.new_map_id(state)
        state.maps[map_id] = sessions.MapSession(
            document=loaded, path=path, disk_sha256=on_disk
        )
    return {
        "map_id": map_id,
        "name": loaded.name,
        "summary": map_summary(loaded),
        "warnings": [warning.as_dict() for warning in warnings],
        "provenance": as_payload(loaded)["provenance"],
        "sha256": sha256_of(serialize_map(loaded)),
    }


def save(
    state: EngineState,
    map_id: str,
    path: str | None = None,
    overwrite: bool = False,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    session = sessions.map_session_for(state, map_id)
    target = (
        path
        if path is not None
        else str(map_service.maps_root() / f"{slugify(session.document.name)}.json")
    )
    if expected_sha256 == "*":
        expected = None
    elif expected_sha256 is not None:
        expected = expected_sha256
    elif session.path == target and session.disk_sha256:
        expected = session.disk_sha256
    else:
        # Nothing this session has seen lives there, so there is no version to
        # be stale against; `overwrite` remains the only guard, as before.
        expected = None
    try:
        saved = map_service.save_file(
            session.document,
            target,
            overwrite=overwrite,
            expected_sha256=expected,
            terrain=sessions.active_registry(state).terrain_effects,
        )
    except (OSError, ValueError) as error:
        raise RequestError(str(error)) from error
    session.path = str(saved["path"])
    # What this session now knows is on disk, so a second save guards against
    # anyone who writes between the two.
    session.disk_sha256 = str(saved["sha256"])
    return {**saved, "map_id": map_id, "provenance": as_payload(session.document)["provenance"]}


def render(
    state: EngineState,
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
    session = sessions.map_session_for(state, map_id)
    tokens: dict[Square, str] = {}
    letters: dict[str, str] = {}
    open_features: list[str] | None = None
    if encounter_id is not None:
        tokens, letters = encounter_tokens(state, session.document, encounter_id)
        # The fight's live fixture states. A mapless fight has none, and a fight
        # on some other map contributes names this document simply does not
        # have — the same leniency the token overlay already takes with a
        # position that lands off the map.
        map_state = sessions.session_for(state, encounter_id).encounter.map_state
        if map_state is not None:
            open_features = sorted(map_state.open_features)
    try:
        rendered = map_service.render_ascii(
            session.document,
            x=x, y=y, width=width, height=height,
            downsample=downsample, show_features=show_features,
            show_elevation=show_elevation, level=level,
            tokens=tokens or None, open=open_features,
        )
    except ValueError as error:
        raise RequestError(str(error)) from error
    result: dict[str, Any] = {"map_id": map_id, "generation": session.generation, **rendered}
    if encounter_id is not None:
        result["tokens"] = letters
    return result


def edit(
    state: EngineState, map_id: str, operations: list[dict[str, Any]]
) -> dict[str, Any]:
    session = sessions.map_session_for(state, map_id)
    before = session.document
    try:
        after = map_service.apply_edits(
            before, operations, terrain=sessions.active_registry(state).terrain_effects
        )
    except ValueError as error:
        raise RequestError(str(error)) from error
    if after is not before:
        session.document = after
        session.generation += 1
    return {
        "applied": len(operations),
        "map_id": map_id,
        "generation": session.generation,
        "edited": after.provenance.edited,
        "summary": map_summary(after),
        "render": edit_render(before, after),
    }


def query_map(
    state: EngineState,
    map_id: str,
    query: str,
    frm: list[int] | None = None,
    to: list[int] | None = None,
    level: int = 0,
) -> dict[str, Any]:
    session = sessions.map_session_for(state, map_id)

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
            session.document, query, origin, target,
            terrain=sessions.active_registry(state).terrain_effects, level=level,
        )
    except (UnknownTerrain, ValueError) as error:
        raise RequestError(str(error)) from error
    return {"map_id": map_id, **answer}


def uvtt_export(
    state: EngineState,
    map_id: str,
    path: str | None = None,
    pixels_per_grid: int = 32,
    include_image: bool = True,
    level: int = 0,
    open_features: list[str] | None = None,
) -> dict[str, Any]:
    session = sessions.map_session_for(state, map_id)
    try:
        payload = uvtt_service.to_uvtt(
            session.document,
            terrain=sessions.active_registry(state).terrain_effects,
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
        else map_service.maps_root() / "uvtt" / f"{slugify(session.document.name)}.uvtt"
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
        "map_id": map_id,
        "resolution": payload["resolution"],
        "wall_polylines": len(payload["line_of_sight"]),
        "portals": len(payload["portals"]),
        "image": include_image,
    }


# --- replays ---------------------------------------------------------------
def replay_validate(bundle: dict[str, Any]) -> dict[str, Any]:
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
        static = resources.files("fivee_sim.editor") / "static"
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
    # in the served /api/replays listing, which reads bundles alone.
    viewer_url = viewer_link(target) if viewer_link is not None else None
    return written if viewer_url is None else {**written, "viewer_url": viewer_url}
