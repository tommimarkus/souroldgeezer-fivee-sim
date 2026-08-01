"""The map service: generate, edit, render, query, and store map documents.

Plain functions over :mod:`fivee_sim.map_document`,
:mod:`fivee_sim.kernel.mapgen`, and :mod:`fivee_sim.kernel.grid`. Every
function takes explicit inputs — a document, a terrain table, a seed — and
raises plain :class:`ValueError` family errors, so the MCP and REST adapters
stay serialization and error mapping only.

Three behaviours worth naming:

**Editing is atomic.** :func:`apply_edits` either returns a new, fully
re-validated document or raises with the index of the operation it refused —
never a document with half the operations applied. The input document is
frozen and untouched either way.

**Rendering is budgeted.** :func:`render_ascii` refuses to emit more than
``RENDER_BUDGET`` cells after downsampling; a huge map is viewed through a
viewport or a coarser downsample, never as a wall of text that drowns the
session it was meant to inform.

**Storage is canonical.** :func:`save_file` writes
:func:`~fivee_sim.map_document.serialize`'s byte-stable text and refuses to
overwrite silently, so a saved map diffs cleanly and a slip of a path cannot
destroy an edited original.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Any, NoReturn

from ..content import CLAUDE_PROJECT_ENV, PROJECT_ENV, contained_json_files
from ..kernel.grid import (
    DiagonalRule,
    Square,
    TerrainTable,
    distance_feet,
    find_path,
    has_line_of_sight,
    square_center,
    step_cost_feet,
    terrain_effect_of,
)
from ..kernel.mapgen import (
    CaveParams,
    DungeonParams,
    OverlandParams,
    generate_caves,
    generate_dungeon,
    generate_overland,
)
from ..map_document import (
    DEFAULT_LEGEND,
    FORMAT,
    FORMAT_VERSION,
    GROUND_LEVEL,
    MAX_MAP_BYTES,
    MAX_MAP_DIM,
    RESERVED_GLYPHS,
    MapDocument,
    MapError,
    MapLevel,
    as_payload,
    canonical_color,
    document_from,
    feature_payload,
    parse_document,
    serialize,
    to_grid,
    validate_document,
)
from ..model.battlemap import MapPlane, SquareClaim
from ..validation import Diagnostic, Severity
from .common import sha256_of
from .errors import MapEditError

__all__ = [
    "MAPS_ENV",
    "RENDER_BUDGET",
    "ResolvedLevel",
    "apply_edits",
    "environment_roots",
    "generate",
    "linked_open_features",
    "list_maps",
    "load_file",
    "maps_root",
    "parse_payload",
    "query",
    "render_ascii",
    "save_file",
]

#: Environment variable holding an ``os.pathsep``-separated list of map files
#: or directories — the maps analogue of ``FIVEE_SIM_CONTENT``.
MAPS_ENV = "FIVEE_SIM_MAPS"
#: Where maps live inside a project when nothing else is configured.
MAPS_SUBDIR = Path(".fivee-sim") / "maps"

#: The hard ceiling on rendered cells after downsampling. Above it, a render
#: is refused with instructions rather than emitted as a wall of text.
RENDER_BUDGET = 10_000


# --- generation -------------------------------------------------------------
_PARAM_TYPES: dict[str, type[CaveParams] | type[DungeonParams] | type[OverlandParams]] = {
    "caves": CaveParams,
    "dungeon": DungeonParams,
    "overland": OverlandParams,
}


def _resolved_params(kind: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
    """The caller's params checked against the kind's dataclass, ready to build.

    Unknown keys are refused with the valid list — the pack-validation rule,
    for the same reason: a mistyped knob silently falling back to its default
    would produce a map that looks deliberate and is not.
    """
    params_type = _PARAM_TYPES[kind]
    defaults = params_type()
    valid = ", ".join(field.name for field in dataclasses.fields(params_type))
    given = dict(params or {})
    unknown = sorted(set(given) - {field.name for field in dataclasses.fields(params_type)})
    if unknown:
        raise ValueError(
            f"unknown {kind} parameter(s): {', '.join(repr(key) for key in unknown)}. "
            f"Valid keys: {valid}"
        )
    resolved: dict[str, Any] = {}
    for key, value in given.items():
        default = getattr(defaults, key)
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{kind} parameter {key!r} must be true or false, got {value!r}")
            resolved[key] = value
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{kind} parameter {key!r} must be a whole number, got {value!r}")
            resolved[key] = value
        else:  # float
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{kind} parameter {key!r} must be a number, got {value!r}")
            resolved[key] = float(value)
    return resolved


def generate(
    kind: str,
    params: Mapping[str, Any] | None,
    seed: int,
    *,
    name: str | None = None,
) -> MapDocument:
    """Generate a map document of ``kind`` — dungeon, caves, or overland.

    ``params`` overrides the kind's defaults; the document's provenance records
    the fully resolved set, so the document alone reproduces the map. The seed
    is required here: choosing one is the caller's affair (see
    :func:`~.common.resolve_seed`), because a service that invented seeds
    silently would break the every-result-reports-its-seed rule.
    """
    if kind not in _PARAM_TYPES:
        valid = ", ".join(sorted(_PARAM_TYPES))
        raise ValueError(f"unknown map kind {kind!r}; valid kinds: {valid}")
    resolved = _resolved_params(kind, params)
    rng = Random(seed)
    built: CaveParams | DungeonParams | OverlandParams
    if kind == "caves":
        built = CaveParams(**resolved)
        generated = generate_caves(rng, built)
    elif kind == "dungeon":
        built = DungeonParams(**resolved)
        generated = generate_dungeon(rng, built)
    else:
        built = OverlandParams(**resolved)
        generated = generate_overland(rng, built)
    return document_from(
        generated,
        name=name if name is not None and name.strip() else f"{kind} {seed}",
        generator=kind,
        seed=seed,
        params=built,
    )


# --- editing ----------------------------------------------------------------
class _Refuse(Exception):
    """Internal: one operation is invalid. The edit loop stamps the index."""


def _refuse(message: str) -> NoReturn:
    raise _Refuse(message)


@dataclass(slots=True)
class _PlaneState:
    """One storey's mutable working copy: everything that varies between levels."""

    name: str
    grid: list[list[str]]
    features: list[dict[str, Any]]
    default_elevation: int
    elevation: dict[Square, int]


@dataclass(slots=True)
class _EditState:
    """The mutable working copy the edit operations act on.

    Everything the document carries has to live here, because
    :func:`apply_edits` rebuilds the payload from this and nothing else — a
    layer left out is a layer every unrelated edit quietly discards. Storeys
    are the newest such layer, which is why ``levels`` holds every plane and
    not only the one an operation happens to name.

    ``target`` is the plane the current operation acts on; the accessors below
    read it, so the handlers say ``state.grid`` and mean "the grid of the level
    this op named" without any of them having to know levels exist.
    """

    name: str
    width: int
    height: int
    legend: dict[str, str]
    levels: dict[int, _PlaneState]
    # Payload form, like the features: the canonical shape goes back out
    # untouched, and the document's own parser is the one arbiter of it.
    palette: dict[str, Any] = dataclasses.field(default_factory=dict)
    target: int = GROUND_LEVEL

    @classmethod
    def from_document(cls, document: MapDocument) -> _EditState:
        payload = as_payload(document)
        storeys = {level["index"]: level for level in payload.get("levels", [])}
        return cls(
            name=document.name,
            width=document.grid.width,
            height=document.grid.height,
            legend=dict(document.legend),
            palette=dict(payload.get("palette", {})),
            levels={
                index: _PlaneState(
                    name=level.name,
                    grid=[list(row) for row in level.tiles],
                    features=list(
                        payload["features"] if index == GROUND_LEVEL
                        else storeys[index]["features"]
                    ),
                    default_elevation=level.elevation.default,
                    elevation=dict(level.elevation.squares),
                )
                for index, level in document.levels.items()
            },
        )

    @property
    def plane(self) -> _PlaneState:
        return self.levels[self.target]

    @property
    def grid(self) -> list[list[str]]:
        return self.plane.grid

    @grid.setter
    def grid(self, rows: list[list[str]]) -> None:
        self.plane.grid = rows

    @property
    def features(self) -> list[dict[str, Any]]:
        return self.plane.features

    @features.setter
    def features(self, entries: list[dict[str, Any]]) -> None:
        self.plane.features = entries

    @property
    def default_elevation(self) -> int:
        return self.plane.default_elevation

    @default_elevation.setter
    def default_elevation(self, feet: int) -> None:
        self.plane.default_elevation = feet

    @property
    def elevation(self) -> dict[Square, int]:
        return self.plane.elevation

    @elevation.setter
    def elevation(self, heights: dict[Square, int]) -> None:
        self.plane.elevation = heights

    def height_at(self, square: Square) -> int:
        return self.elevation.get(square, self.default_elevation)

    def feature_ids(self) -> set[str]:
        """Every id in use anywhere on the map — ids are unique document-wide."""
        return {
            str(entry["id"])
            for plane in self.levels.values()
            for entry in plane.features
        }


_EDIT_OPS = (
    "add_feature", "adjust_elevation", "carve_corridor", "line", "paint",
    "remove_feature", "resize", "set_elevation", "set_feature", "set_legend",
    "set_name", "set_palette", "set_terrain", "toggle_door",
)

#: The ops that act on one storey and so accept a ``level``. The rest —
#: ``set_name``, ``set_legend``, ``set_palette``, ``resize`` — are document-wide
#: by nature, and taking a level would suggest they could be applied to one
#: floor alone. ``set_feature`` is the fifth, for a different reason: it edits
#: the feature its record's id names, wherever that feature stands, and taking a
#: level would be taking the power to rehouse a fixture on another storey — the
#: exact silent relocation the remove-and-re-add pair it replaces could do.
_LEVELLED_OPS = frozenset({
    "add_feature", "adjust_elevation", "carve_corridor", "line", "paint",
    "remove_feature", "set_elevation", "set_terrain", "toggle_door",
})
_OP_KEYS: dict[str, frozenset[str]] = {
    "add_feature": frozenset({"feature"}),
    "adjust_elevation": frozenset({"rect", "cells", "by"}),
    "carve_corridor": frozenset({"from", "to", "terrain", "horizontal_first"}),
    "line": frozenset({"from", "to", "terrain"}),
    "paint": frozenset({"cells", "terrain"}),
    "remove_feature": frozenset({"id"}),
    "resize": frozenset({"width", "height", "anchor", "fill"}),
    "set_elevation": frozenset({"rect", "cells", "feet", "default"}),
    "set_feature": frozenset({"feature"}),
    "set_legend": frozenset({"glyph", "terrain"}),
    "set_name": frozenset({"name"}),
    "set_palette": frozenset({"terrain", "color"}),
    "set_terrain": frozenset({"rect", "terrain"}),
    "toggle_door": frozenset({"at"}),
}
_OP_KEYS = {
    name: keys | {"level"} if name in _LEVELLED_OPS else keys
    for name, keys in _OP_KEYS.items()
}
#: What ``add_feature`` and ``set_feature`` accept — the document's own feature
#: keys, entire. The seven after ``to_level`` are the fixture keys: what operating
#: a feature changes, needs, costs and rolls. They ride through to
#: :func:`apply_edits`' final ``parse_document``, which is the one arbiter of
#: them, exactly as ``palette`` entries do; only an overlay's *shape* is checked
#: here, and only because a ``rect`` has to be expanded before the payload.
#:
#: ``to_level`` is the key that makes a storey walkable, and until it was listed
#: here no operation could write one — a connector could only be authored by
#: hand-editing the file. Which level it may name, and that it may never name
#: its own, stays the document's to refuse.
_FEATURE_FIELDS = frozenset(
    {
        "id", "kind", "at", "orientation", "hinge", "swing", "state",
        "linked_to", "team", "to_level",
        "terrain", "elevation", "affects", "requires", "costs_action", "check",
        "trigger",
    }
)
#: The keys that need no shaping at all: copied across as written.
_PASSED_THROUGH = (
    "hinge", "swing", "linked_to", "to_level", "terrain", "elevation",
    "requires", "trigger", "costs_action", "check",
)
#: Said on every ``set_feature`` refusal a merge-shaped call trips, because the
#: difference is otherwise silent: a caller who believes the op merges writes
#: the one key they mean to change, and a replace that honoured it would return
#: a record holding only that key. Every such call omits ``kind`` or ``at``, so
#: every such call is refused — and this is where it learns which it got.
_REPLACES_WHOLE = (
    "set_feature writes the record whole: a key left out is a key removed, not "
    "a key kept, so name every field the feature is to keep — or use "
    "toggle_door, which flips a door's state in place"
)
_ANCHORS = ("top-left", "top-right", "bottom-left", "bottom-right")
_DOOR_ORIENTATIONS = ("horizontal", "vertical")
_DOOR_STATES = ("open", "closed")


def _square_value(value: Any, what: str, state: _EditState) -> Square:
    if value is None:
        _refuse(f"{what} is required")
    if not (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(part, int) and not isinstance(part, bool) for part in value)
    ):
        _refuse(f"{what} must be [x, y] square indices, got {value!r}")
    square = (int(value[0]), int(value[1]))
    if not (0 <= square[0] < state.width and 0 <= square[1] < state.height):
        _refuse(
            f"{what} is [{square[0]}, {square[1]}], outside the "
            f"{state.width}x{state.height} map"
        )
    return square


def _glyph_of_kind(state: _EditState, kind: str) -> str:
    """The glyph that paints ``kind``, or a refusal that says how to get one."""
    for glyph, named in state.legend.items():
        if named == kind:
            return glyph
    legend = ", ".join(f"{glyph!r}={named}" for glyph, named in state.legend.items()) or "empty"
    _refuse(
        f"terrain {kind!r} has no glyph in this document's legend ({legend}); "
        f"add one with set_legend first"
    )


def _op_terrain(
    state: _EditState, op: Mapping[str, Any], key: str, default: str | None = None
) -> str:
    value = op.get(key, default)
    if value is None:
        _refuse(f"{key!r} is required: name a terrain kind")
    if not isinstance(value, str) or not value.strip():
        _refuse(f"{key!r} must name a terrain kind, got {value!r}")
    return _glyph_of_kind(state, value)


def _terrain_kind(op: Mapping[str, Any], terrain: TerrainTable) -> str:
    """The op's ``terrain``, checked against the active content rather than the legend.

    What :func:`_op_terrain` does for the ops that *paint* a kind, for the ops
    that merely *name* one — a document may color or legend a kind it has not
    put on the map.
    """
    kind = op.get("terrain")
    if not isinstance(kind, str) or not kind.strip():
        _refuse(f"'terrain' must name a terrain kind, got {kind!r}")
    if kind not in terrain:
        available = ", ".join(sorted(terrain)) or "none"
        _refuse(f"terrain {kind!r} is not defined by the active content. Available: {available}")
    return kind


def _bresenham(start: Square, end: Square) -> list[Square]:
    """The classic Bresenham raster from ``start`` to ``end``, inclusive."""
    x0, y0 = start
    x1, y1 = end
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    out: list[Square] = []
    while True:
        out.append((x0, y0))
        if (x0, y0) == (x1, y1):
            return out
        doubled = 2 * err
        if doubled >= dy:
            err += dy
            x0 += sx
        if doubled <= dx:
            err += dx
            y0 += sy


def _span(a: int, b: int) -> range:
    return range(a, b + 1) if a <= b else range(a, b - 1, -1)


def _l_corridor(start: Square, end: Square, horizontal_first: bool) -> list[Square]:
    x0, y0 = start
    x1, y1 = end
    if horizontal_first:
        return [(xx, y0) for xx in _span(x0, x1)] + [(x1, yy) for yy in _span(y0, y1)]
    return [(x0, yy) for yy in _span(y0, y1)] + [(xx, y1) for xx in _span(x0, x1)]


def _rect_squares(state: _EditState, op: Mapping[str, Any]) -> list[Square]:
    """Every square of a validated ``rect``, in row-major order."""
    raw = op.get("rect")
    if not (
        isinstance(raw, (list, tuple))
        and len(raw) == 4
        and all(isinstance(part, int) and not isinstance(part, bool) for part in raw)
    ):
        _refuse(f"'rect' must be [x, y, width, height] in squares, got {raw!r}")
    rx, ry, rw, rh = (int(part) for part in raw)
    if rw < 1 or rh < 1:
        _refuse(f"'rect' width and height must be at least 1, got {rw}x{rh}")
    if not (0 <= rx and 0 <= ry and rx + rw <= state.width and ry + rh <= state.height):
        _refuse(
            f"'rect' [{rx}, {ry}, {rw}, {rh}] reaches outside the "
            f"{state.width}x{state.height} map"
        )
    return [(xx, yy) for yy in range(ry, ry + rh) for xx in range(rx, rx + rw)]


def _op_set_terrain(state: _EditState, op: Mapping[str, Any], terrain: TerrainTable) -> None:
    squares = _rect_squares(state, op)
    glyph = _op_terrain(state, op, "terrain")
    for xx, yy in squares:
        state.grid[yy][xx] = glyph


def _op_paint(state: _EditState, op: Mapping[str, Any], terrain: TerrainTable) -> None:
    raw = op.get("cells")
    if not isinstance(raw, list) or not raw:
        _refuse("'cells' must be a non-empty list of [x, y] squares")
    squares = [_square_value(cell, "each cell", state) for cell in raw]
    glyph = _op_terrain(state, op, "terrain")
    for xx, yy in squares:
        state.grid[yy][xx] = glyph


def _op_line(state: _EditState, op: Mapping[str, Any], terrain: TerrainTable) -> None:
    start = _square_value(op.get("from"), "'from'", state)
    end = _square_value(op.get("to"), "'to'", state)
    glyph = _op_terrain(state, op, "terrain")
    for xx, yy in _bresenham(start, end):
        state.grid[yy][xx] = glyph


def _op_carve_corridor(state: _EditState, op: Mapping[str, Any], terrain: TerrainTable) -> None:
    start = _square_value(op.get("from"), "'from'", state)
    end = _square_value(op.get("to"), "'to'", state)
    horizontal_first = op.get("horizontal_first", True)
    if not isinstance(horizontal_first, bool):
        _refuse(f"'horizontal_first' must be true or false, got {horizontal_first!r}")
    glyph = _op_terrain(state, op, "terrain", default="floor")
    for xx, yy in _l_corridor(start, end, horizontal_first):
        state.grid[yy][xx] = glyph


def _overlay_entries(state: _EditState, raw: Any) -> list[dict[str, Any]]:
    """One feature's ``affects`` groups, with any ``rect`` expanded to its cells.

    The document stores **cells and never a rect**: a rect is the author's
    shorthand — the :func:`_op_set_elevation` precedent, and the difference
    between typing one rect and forty pairs — so expanding it here leaves the
    file one shape, which is what a resize has to translate square by square and
    what ``map_document`` refuses ``rect`` in. Which squares an overlay names is
    the only question answered here; what it does to them rides through.
    """
    if not isinstance(raw, list):
        _refuse(
            "'affects' must be a list of overlay objects, each naming the cells it "
            "governs and what they are in each state"
        )
    groups: list[dict[str, Any]] = []
    for index, group in enumerate(raw):
        if not isinstance(group, Mapping):
            _refuse(f"'affects' entry #{index} must be an object")
        squares = _height_targets(state, group, f"'affects' entry #{index}")
        rest = {key: value for key, value in group.items() if key not in ("rect", "cells")}
        groups.append(
            {
                "cells": [
                    [square[0], square[1]]
                    for square in sorted(squares, key=lambda s: (s[1], s[0]))
                ],
                **rest,
            }
        )
    return groups


def _feature_and_id(raw: Any, note: str) -> tuple[Mapping[str, Any], str]:
    """A feature record and the id in it, or the refusal that says what is wrong.

    Shared by both feature-writing ops because the id is the one key neither can
    do without: ``add_feature`` names what it is creating, ``set_feature`` names
    what it is editing. Both are answered before either op acts, so the id is
    known even to the refusals that come after it.
    """
    if not isinstance(raw, Mapping):
        _refuse(
            "'feature' must be an object: {id, kind, at, orientation?, state?, "
            "team?, to_level?} and, for a fixture, terrain, elevation, affects, "
            f"requires, costs_action, check{note}"
        )
    unknown = sorted(set(raw) - _FEATURE_FIELDS)
    if unknown:
        _refuse(
            f"feature has unknown key(s): {', '.join(repr(key) for key in unknown)}. "
            f"Valid keys: {', '.join(sorted(_FEATURE_FIELDS))}"
        )
    feature_id = raw.get("id")
    if not isinstance(feature_id, str) or not feature_id.strip():
        _refuse("feature 'id' is required and must be non-empty text")
    return raw, feature_id


def _feature_entry(
    state: _EditState, raw: Mapping[str, Any], feature_id: str, *, note: str = ""
) -> dict[str, Any]:
    """One feature record in payload form, shaped as far as the service goes.

    The whole record, always: both ops that write a feature write every field
    the call names and nothing else, which is what lets the final
    ``parse_document`` be the one arbiter of what the fields *mean*. ``note``
    rides on the refusals a caller who expected ``set_feature`` to merge would
    trip — see :data:`_REPLACES_WHOLE` for why they are the ones that carry it.
    """
    kind = raw.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        _refuse(f"feature 'kind' is required and must be non-empty text{note}")
    if raw.get("at") is None:
        _refuse(f"feature 'at' is required{note}")
    at = _square_value(raw.get("at"), "feature 'at'", state)
    orientation = raw.get("orientation")
    if orientation is not None and orientation not in _DOOR_ORIENTATIONS:
        _refuse(
            f"feature 'orientation' must be one of: {', '.join(_DOOR_ORIENTATIONS)}; "
            f"got {orientation!r}"
        )
    door_state = raw.get("state")
    if door_state is not None and door_state not in _DOOR_STATES:
        _refuse(f"feature 'state' must be one of: {', '.join(_DOOR_STATES)}; got {door_state!r}")
    team = raw.get("team")
    if team is not None and (not isinstance(team, str) or not team.strip()):
        _refuse(f"feature 'team' must be non-empty text, got {team!r}")
    if kind == "door":
        if orientation is None:
            _refuse(f"a door needs 'orientation' (horizontal or vertical){note}")
        if door_state is None:
            _refuse(
                "a door needs 'state' (open or closed); the document stores the "
                f"default{note}"
            )
    entry: dict[str, Any] = {"id": feature_id, "kind": kind, "at": [at[0], at[1]]}
    if orientation is not None:
        entry["orientation"] = orientation
    if door_state is not None:
        entry["state"] = door_state
    if team is not None:
        entry["team"] = team
    for key in _PASSED_THROUGH:
        if key in raw:
            entry[key] = raw[key]
    if "affects" in raw:
        entry["affects"] = _overlay_entries(state, raw["affects"])
    return entry


def _op_add_feature(state: _EditState, op: Mapping[str, Any], terrain: TerrainTable) -> None:
    raw, feature_id = _feature_and_id(op.get("feature"), "")
    if feature_id in state.feature_ids():
        _refuse(
            f"a feature named {feature_id!r} already exists; ids must be unique. "
            f"set_feature edits the one that is there"
        )
    state.features.append(_feature_entry(state, raw, feature_id))


def _op_set_feature(state: _EditState, op: Mapping[str, Any], terrain: TerrainTable) -> None:
    """Edit one feature in place, by id, replacing its record.

    **Replace, not merge**, and the choice is load-bearing rather than
    incidental. Every other ``set_*`` op in this module states a value rather
    than patching one, and replacement is what makes this one's result a
    function of the call alone: a merge would make it depend on state the call
    never names, and — with no delete convention anywhere in the feature keys —
    would leave a fixture's ``affects``, ``requires``, ``trigger``, or ``check``
    impossible to clear at all. The cost is that a key left out is a key removed,
    which is why
    :data:`_REPLACES_WHOLE` rides on every refusal a merge-shaped call trips.

    Position and identity are what it preserves: the record goes back at the
    index it came from, on the plane it was already on, under the id that found
    it. That is the whole gap it closes — ``remove_feature`` plus
    ``add_feature`` reorders the array, and ``add_feature`` takes a ``level``,
    so the pair could silently rehouse a fixture one storey up.
    """
    note = f". {_REPLACES_WHOLE}"
    raw, feature_id = _feature_and_id(op.get("feature"), note)
    for plane in state.levels.values():
        for index, entry in enumerate(plane.features):
            if entry["id"] == feature_id:
                plane.features[index] = _feature_entry(state, raw, feature_id, note=note)
                return
    known = ", ".join(sorted(state.feature_ids())) or "none"
    _refuse(f"no feature named {feature_id!r}; features: {known}")


def _op_remove_feature(state: _EditState, op: Mapping[str, Any], terrain: TerrainTable) -> None:
    feature_id = op.get("id")
    if not isinstance(feature_id, str) or not feature_id.strip():
        _refuse("'id' must name the feature to remove")
    for index, entry in enumerate(state.features):
        if entry["id"] == feature_id:
            linked_to = entry.get("linked_to")
            if linked_to is not None:
                _refuse(
                    f"{feature_id!r} is linked to {str(linked_to)!r}; unlink both doors "
                    "with set_feature before removing either leaf"
                )
            del state.features[index]
            return
    known = ", ".join(entry["id"] for entry in state.features) or "none"
    _refuse(f"no feature named {feature_id!r}; features: {known}")


def _op_toggle_door(state: _EditState, op: Mapping[str, Any], terrain: TerrainTable) -> None:
    at = _square_value(op.get("at"), "'at'", state)
    for entry in state.features:
        if entry.get("kind") == "door" and entry.get("at") == [at[0], at[1]]:
            new_state = "closed" if entry.get("state") == "open" else "open"
            entry["state"] = new_state
            linked_to = entry.get("linked_to")
            if linked_to is not None:
                for partner in state.features:
                    if partner.get("id") == linked_to:
                        partner["state"] = new_state
                        break
            return
    doors = ", ".join(
        str(entry["at"]) for entry in state.features if entry.get("kind") == "door"
    ) or "none"
    _refuse(f"no door at [{at[0]}, {at[1]}]; doors: {doors}")


def _resized_overlays(
    entry: Mapping[str, Any], off_x: int, off_y: int, new_w: int, new_h: int
) -> list[dict[str, Any]]:
    """One feature's ``affects`` groups in the new frame, cropped to it.

    A layer nested inside a record is still a layer. Overlay cells carry
    coordinates, so a resize that moved only ``at`` would leave them
    untranslated and mislocate the flood by exactly the anchor offset — on
    maps somebody had resized and nowhere else. Cells outside the new bounds go
    with the squares they described, as height does; a group emptied that way
    is dropped, and the fixture keeps its own square.
    """
    groups: list[dict[str, Any]] = []
    for group in entry.get("affects") or ():
        cells = [
            [x + off_x, y + off_y]
            for x, y in group["cells"]
            if 0 <= x + off_x < new_w and 0 <= y + off_y < new_h
        ]
        if cells:
            cells.sort(key=lambda cell: (cell[1], cell[0]))
            groups.append({**group, "cells": cells})
    return groups


def _refuse_orphaned_prerequisites(
    state: _EditState, off_x: int, off_y: int, new_w: int, new_h: int
) -> None:
    """Refuse a resize that drops a fixture a surviving fixture requires.

    A fixture pushed off the map is dropped, as one always has been. One
    another fixture *requires* cannot be: the final re-parse would refuse the
    whole edit naming a prerequisite that is missing, rather than the resize
    that removed it. ``editor/static/editor.html`` refuses the same case in its
    own resize, and the two are meant to agree.
    """
    def survives(entry: Mapping[str, Any]) -> bool:
        fx, fy = entry["at"]
        return 0 <= int(fx) + off_x < new_w and 0 <= int(fy) + off_y < new_h

    features = [
        entry for index in sorted(state.levels) for entry in state.levels[index].features
    ]
    # Prerequisites cross storeys — which floor the thing a fixture waits on
    # stands on is the fiction's business — so the table spans the document.
    by_id = {str(entry["id"]): entry for entry in features}
    for entry in features:
        if not survives(entry):
            continue
        linked_to = entry.get("linked_to")
        linked = by_id.get(str(linked_to)) if linked_to is not None else None
        if linked is not None and not survives(linked):
            _refuse(
                f"resizing would push {str(linked_to)!r} off the map, and "
                f"{str(entry['id'])!r} is linked to it; unlink or move the pair first"
            )
        for wanted in entry.get("requires") or ():
            other = by_id.get(str(wanted))
            if other is not None and not survives(other):
                _refuse(
                    f"resizing would push {str(wanted)!r} off the map, and "
                    f"{str(entry['id'])!r} requires it; move or remove it first"
                )
        trigger = entry.get("trigger")
        when = trigger.get("when") if isinstance(trigger, Mapping) else None
        if isinstance(when, Mapping):
            for wanted in when:
                other = by_id.get(str(wanted))
                if other is not None and not survives(other):
                    _refuse(
                        f"resizing would push {str(wanted)!r} off the map, and "
                        f"{str(entry['id'])!r}'s trigger observes it; move or remove "
                        "the observing fixture first"
                    )


def _op_resize(state: _EditState, op: Mapping[str, Any], terrain: TerrainTable) -> None:
    dims: dict[str, int] = {}
    for key in ("width", "height"):
        value = op.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            _refuse(f"{key!r} is required and must be a whole number of squares")
        if not 1 <= value <= MAX_MAP_DIM:
            _refuse(f"{key!r} must be between 1 and {MAX_MAP_DIM}, got {value}")
        dims[key] = value
    anchor = op.get("anchor", "top-left")
    if anchor not in _ANCHORS:
        _refuse(f"'anchor' must be one of: {', '.join(_ANCHORS)}; got {anchor!r}")
    fill_kind = op.get("fill", "wall")
    if not isinstance(fill_kind, str) or not fill_kind.strip():
        _refuse(f"'fill' must name a terrain kind, got {fill_kind!r}")
    fill = _glyph_of_kind(state, fill_kind)

    new_w, new_h = dims["width"], dims["height"]
    off_x = 0 if "left" in anchor else new_w - state.width
    off_y = 0 if anchor.startswith("top") else new_h - state.height
    _refuse_orphaned_prerequisites(state, off_x, off_y, new_w, new_h)
    # A frame change is document-wide: every storey shares the grid, so a resize
    # that translated the ground alone would leave the floors above it
    # mislocated over the map they belong to.
    for plane in state.levels.values():
        grid: list[list[str]] = []
        for yy in range(new_h):
            row: list[str] = []
            for xx in range(new_w):
                ox, oy = xx - off_x, yy - off_y
                if 0 <= ox < state.width and 0 <= oy < state.height:
                    row.append(plane.grid[oy][ox])
                else:
                    row.append(fill)
            grid.append(row)
        kept: list[dict[str, Any]] = []
        for entry in plane.features:
            fx, fy = entry["at"]
            nx, ny = fx + off_x, fy + off_y
            if not (0 <= nx < new_w and 0 <= ny < new_h):
                continue
            moved = {**entry, "at": [nx, ny]}
            groups = _resized_overlays(entry, off_x, off_y, new_w, new_h)
            if groups:
                moved["affects"] = groups
            else:
                moved.pop("affects", None)
            kept.append(moved)
        # Height moves with the anchor exactly as tiles and features do; ground
        # that falls outside the new bounds goes with the squares it described,
        # and new ground comes in at the plane's datum.
        plane.grid = grid
        plane.features = kept
        plane.elevation = {
            (fx + off_x, fy + off_y): feet
            for (fx, fy), feet in plane.elevation.items()
            if 0 <= fx + off_x < new_w and 0 <= fy + off_y < new_h
        }
    state.width, state.height = new_w, new_h


def _height_targets(state: _EditState, op: Mapping[str, Any], what: str) -> list[Square]:
    """The squares a height op names — exactly one of ``rect`` or ``cells``."""
    has_rect = "rect" in op
    has_cells = "cells" in op
    if has_rect == has_cells:
        _refuse(
            f"{what} needs exactly one of 'rect' ([x, y, width, height]) or "
            f"'cells' (a list of [x, y] squares)"
        )
    if has_rect:
        return _rect_squares(state, op)
    raw = op.get("cells")
    if not isinstance(raw, list) or not raw:
        _refuse("'cells' must be a non-empty list of [x, y] squares")
    return [_square_value(cell, "each cell", state) for cell in raw]


def _height_feet(op: Mapping[str, Any], key: str) -> int:
    value = op.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        _refuse(f"{key!r} is required and must be a whole number of feet, got {value!r}")
    return int(value)


def _set_height(state: _EditState, square: Square, feet: int) -> None:
    """Record one square's height, keeping the layer sparse against the default."""
    if feet == state.default_elevation:
        state.elevation.pop(square, None)
    else:
        state.elevation[square] = feet


def _op_set_elevation(state: _EditState, op: Mapping[str, Any], terrain: TerrainTable) -> None:
    """Set ground height outright: a plateau, a pit floor, or the whole map's datum.

    ``default`` alone moves the datum every unnamed square sits at, which is how
    a map is lifted or sunk as a whole without listing every square.
    """
    if "default" in op:
        if "rect" in op or "cells" in op:
            _refuse(
                "'default' moves the datum every unnamed square sits at and cannot "
                "be combined with 'rect' or 'cells'; use two operations"
            )
        state.default_elevation = _height_feet(op, "default")
        # Squares that named the new datum explicitly are now redundant.
        for square in list(state.elevation):
            _set_height(state, square, state.elevation[square])
        return
    squares = _height_targets(state, op, "set_elevation")
    feet = _height_feet(op, "feet")
    for square in squares:
        _set_height(state, square, feet)


def _op_adjust_elevation(
    state: _EditState, op: Mapping[str, Any], terrain: TerrainTable
) -> None:
    """Raise or lower named squares by ``by`` feet, relative to what they are now."""
    squares = _height_targets(state, op, "adjust_elevation")
    by = _height_feet(op, "by")
    for square in squares:
        _set_height(state, square, state.height_at(square) + by)


def _op_set_legend(state: _EditState, op: Mapping[str, Any], terrain: TerrainTable) -> None:
    glyph = op.get("glyph")
    if not isinstance(glyph, str) or len(glyph) != 1:
        _refuse(f"'glyph' must be a single character, got {glyph!r}")
    if glyph in RESERVED_GLYPHS:
        _refuse(
            f"glyph {glyph!r} is reserved for renderer overlays "
            f"({' '.join(sorted(RESERVED_GLYPHS))}) and cannot name terrain"
        )
    state.legend[glyph] = _terrain_kind(op, terrain)


def _color_value(value: Any) -> Any:
    """One palette value in document payload form, refusing anything else.

    The syntax rule itself is :func:`~fivee_sim.map_document.canonical_color`, so
    an edit operation and a hand-written file cannot drift apart on what a color
    is; only the shape around it — a pair, or one color for both themes — is
    unpacked here.
    """
    if isinstance(value, str):
        canonical = canonical_color(value)
        if canonical is None:
            _refuse(_bad_color(value))
        return canonical
    if isinstance(value, Mapping):
        unknown = sorted(set(value) - {"light", "dark"})
        if unknown:
            _refuse(
                f"a color pair takes only 'light' and 'dark'; got "
                f"{', '.join(repr(key) for key in unknown)}"
            )
        pair: dict[str, str] = {}
        for theme in ("light", "dark"):
            raw = value.get(theme)
            if raw is None:
                _refuse(
                    'a color must give both "light" and "dark", or a single color '
                    "for both themes"
                )
            canonical = canonical_color(raw) if isinstance(raw, str) else None
            if canonical is None:
                _refuse(_bad_color(raw))
            pair[theme] = canonical
        return pair
    _refuse(_bad_color(value))


def _bad_color(value: Any) -> str:
    return (
        f"'color' must be a hex color like \"#d2440f\", a {{light, dark}} pair of "
        f"them, or null to clear the kind's color; got {value!r}"
    )


def _op_set_palette(state: _EditState, op: Mapping[str, Any], terrain: TerrainTable) -> None:
    kind = _terrain_kind(op, terrain)
    if op.get("color") is None:
        state.palette.pop(kind, None)
        return
    state.palette[kind] = _color_value(op["color"])


def _op_set_name(state: _EditState, op: Mapping[str, Any], terrain: TerrainTable) -> None:
    name = op.get("name")
    if not isinstance(name, str) or not name.strip():
        _refuse(f"'name' must be non-empty text, got {name!r}")
    state.name = name


_HANDLERS: dict[str, Any] = {
    "add_feature": _op_add_feature,
    "adjust_elevation": _op_adjust_elevation,
    "carve_corridor": _op_carve_corridor,
    "line": _op_line,
    "paint": _op_paint,
    "remove_feature": _op_remove_feature,
    "resize": _op_resize,
    "set_elevation": _op_set_elevation,
    "set_feature": _op_set_feature,
    "set_legend": _op_set_legend,
    "set_name": _op_set_name,
    "set_palette": _op_set_palette,
    "set_terrain": _op_set_terrain,
    "toggle_door": _op_toggle_door,
}


def _height_payload(plane: _PlaneState) -> dict[str, Any]:
    """One plane's heights, sorted by row then column as the document writes them."""
    return {
        "default": plane.default_elevation,
        "squares": [
            [square[0], square[1], plane.elevation[square]]
            for square in sorted(plane.elevation, key=lambda s: (s[1], s[0]))
        ],
    }


def _apply_one(state: _EditState, operation: Any, terrain: TerrainTable) -> None:
    valid = ", ".join(_EDIT_OPS)
    if not isinstance(operation, Mapping):
        _refuse(f"an operation must be an object with an 'op' key; valid ops: {valid}")
    name = operation.get("op")
    if not isinstance(name, str) or name not in _HANDLERS:
        _refuse(f"unknown op {name!r}; valid ops: {valid}")
    unknown = sorted(set(operation) - _OP_KEYS[name] - {"op"})
    if unknown:
        _refuse(
            f"{name} has unknown key(s): {', '.join(repr(key) for key in unknown)}. "
            f"Valid keys: {', '.join(sorted(_OP_KEYS[name]))}"
        )
    level = operation.get("level", GROUND_LEVEL)
    if isinstance(level, bool) or not isinstance(level, int):
        _refuse(f"'level' must name a storey by whole number, got {level!r}")
    if level not in state.levels:
        declared = ", ".join(str(index) for index in sorted(state.levels))
        _refuse(f"there is no level {level} on this map. Levels: {declared}")
    state.target = level
    _HANDLERS[name](state, operation, terrain)


def apply_edits(
    document: MapDocument,
    operations: Sequence[Mapping[str, Any]],
    *,
    terrain: TerrainTable,
) -> MapDocument:
    """Apply edit operations atomically: a new document, or nothing at all.

    A bad operation raises :class:`MapEditError` naming its index; the input
    document — frozen — is untouched either way. The composed result goes back
    through :func:`~fivee_sim.map_document.parse_document` before it is
    returned, because a sequence of individually valid operations can still
    compose an invalid document, and that is refused whole.
    ``provenance.edited`` flips to true only when the document actually
    changed; an edit that lands the map exactly where it stood returns the
    original object.
    """
    state = _EditState.from_document(document)
    for index, operation in enumerate(operations):
        try:
            _apply_one(state, operation, terrain)
        except _Refuse as refusal:
            raise MapEditError(index, str(refusal)) from None

    provenance = dict(as_payload(document)["provenance"])
    payload = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "name": state.name,
        "grid": {
            "width": state.width,
            "height": state.height,
            "cell_feet": document.grid.cell_feet,
        },
        "legend": dict(state.legend),
        "palette": dict(state.palette),
        "tiles": ["".join(row) for row in state.levels[GROUND_LEVEL].grid],
        "elevation": _height_payload(state.levels[GROUND_LEVEL]),
        "features": state.levels[GROUND_LEVEL].features,
        "provenance": provenance,
    }
    storeys = [
        {
            "index": index,
            "name": state.levels[index].name,
            "tiles": ["".join(row) for row in state.levels[index].grid],
            "elevation": _height_payload(state.levels[index]),
            "features": state.levels[index].features,
        }
        for index in sorted(state.levels)
        if index != GROUND_LEVEL
    ]
    if storeys:
        payload["levels"] = storeys
    result = parse_document(payload, source=document.name, terrain=terrain)
    if serialize(result) == serialize(document):
        return document
    return dataclasses.replace(
        result, provenance=dataclasses.replace(result.provenance, edited=True)
    )


# --- what the fixtures make of a square -------------------------------------
def linked_open_features(
    plane: MapPlane, open_features: Collection[str]
) -> frozenset[str]:
    """Expand either leaf of a linked door pair to their shared live state.

    Unknown names and names belonging to another level stay untouched: an
    encounter owns one map-wide set, while this resolver deliberately knows
    about one plane. The document and encounter validators guarantee that a
    link is reciprocal and has exactly two leaves.
    """
    resolved = set(open_features)
    for feature_id in tuple(resolved):
        feature = plane.features.get(feature_id)
        if feature is not None and feature.linked_to is not None:
            resolved.add(feature.linked_to)
    return frozenset(resolved)


@dataclass(frozen=True, slots=True)
class ResolvedLevel:
    """One storey's squares as a given set of open fixtures leaves them.

    The single reader-side answer to "given a plane and which fixtures stand
    open, what is each square": :func:`query` asks it of the states the document
    authored, :func:`render_ascii` of the states a fight is in,
    :func:`~fivee_sim.service.uvtt.to_uvtt` of the states an export is asked
    for, and they share this so the three cannot answer differently. The claims
    come from :meth:`~fivee_sim.model.battlemap.MapFeature.claims`, whose own
    docstring names deriving them twice as how answers drift, and
    ``Encounter._adopt_map`` builds the live index from that same call.

    Public for that third reader: ``uvtt`` is a sibling in this layer, not a
    caller of the map service, so it needs the derivation by name rather than
    through a function that renders.

    The exactly-one-fixture-per-square rule is what makes the index total: with
    no precedence to settle there is no document order to consult and no history
    to replay, so a reader cannot disagree with a fight about a square.

    These mirror ``Encounter``'s composers (``_terrain_at``, ``_elevation_at``)
    minus the encounter-only parts — nobody occupies anything here. A claim
    missing a pair falls through as an unclaimed square does: a fixture that
    only moves a water level leaves the ground it finds, and one that only
    floods a room leaves the height it finds.
    """

    plane: MapPlane
    claims: Mapping[Square, SquareClaim]
    open_features: frozenset[str]

    @classmethod
    def of(cls, plane: MapPlane, open_features: Collection[str]) -> ResolvedLevel:
        """The plane resolved through the fixtures named open.

        Names the plane has no fixture for are ignored rather than refused: a
        fight's set spans every storey, and which floor a fixture stands on is
        not this level's business.
        """
        return cls(
            plane=plane,
            claims={
                square: claim
                for feature in plane.features.values()
                for square, claim in feature.claims()
            },
            open_features=linked_open_features(plane, open_features),
        )

    def terrain_at(self, square: Square) -> str:
        claim = self.claims.get(square)
        if claim is not None and claim.terrain is not None:
            pair = claim.terrain
            return pair.open if claim.feature in self.open_features else pair.closed
        return self.plane.terrain.get(square, self.plane.default_terrain)

    def height_at(self, square: Square) -> int:
        claim = self.claims.get(square)
        if claim is not None and claim.elevation is not None:
            feet = claim.elevation
            return feet.open if claim.feature in self.open_features else feet.closed
        return self.plane.elevation.get(square, self.plane.default_elevation)


# --- rendering --------------------------------------------------------------
def _majority(counts: Mapping[str, int], order_of: Mapping[str, int]) -> str:
    """The most common kind; ties go to the kind first named in the legend."""
    return min(counts, key=lambda kind: (-counts[kind], order_of[kind]))


def _lowest_majority(counts: Mapping[int, int]) -> int:
    """The most common height in a block; ties go to the lower ground."""
    return min(counts, key=lambda feet: (-counts[feet], feet))


#: Height glyphs in ascending order, so a rendered relief reads as a contour: the
#: lowest ground in view is always ``0``. The legend gives each one its feet.
HEIGHT_GLYPHS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _feature_glyph(kind: str, state: str | None) -> str | None:
    """The overlay a feature draws, or ``None`` if it draws nothing.

    The three drawn annotations keep their glyphs even when they can be
    operated: a glyph says what a thing *is*, and an operable hatch drawing
    ``+`` would read as a door in the corridor. Its state is not lost by that —
    ``features_in_view`` carries the whole record.

    Anything else carrying a state draws as that state, ``+`` shut and ``/``
    open, because carrying a state is what makes a feature a fixture the fight
    operates: a spike or a sluice is visible rather than invisible for not
    being a door.

    Deliberately **no sixth reserved glyph**: :data:`RESERVED_GLYPHS` is closed,
    a legend claiming one is a validation error, and adding to the set would
    retroactively invalidate every document that legends that character today.
    """
    annotation = {"stairs_up": "<", "stairs_down": ">", "spawn": "@"}.get(kind)
    if annotation is not None:
        return annotation
    if state is not None:
        return "/" if state == "open" else "+"
    return None


#: Characters a render may borrow, in order, for a terrain kind that a fixture
#: puts on the map and neither the document's legend nor :data:`DEFAULT_LEGEND`
#: gives a free glyph. Punctuation only, and none of it reserved: an adapter's
#: combatant marks are letters, so a borrowed glyph can never be read as a token.
SPARE_GLYPHS = "=&$*!?:;,'()[]{}|-_"

#: :data:`DEFAULT_LEGEND` the way a render asks it — kind to glyph. The format's
#: own legend names each kind once, so the reversal is lossless.
_FORMAT_GLYPH_OF: Mapping[str, str] = {
    kind: glyph for glyph, kind in DEFAULT_LEGEND.items()
}


def _kind_glyphs(
    document: MapDocument,
    live: ResolvedLevel | None,
    taken: Collection[str],
) -> tuple[dict[str, str], dict[str, int]]:
    """A glyph and a tie-break rank for every terrain kind a render can draw.

    The document's legend, reversed, comes first and always wins: a kind the map
    is already read in keeps the glyph it is read in. That covers every square
    when no fixture state is in force, which is why ``live is None`` returns here.

    A fixture's terrain kinds are resolved against **loaded content, not this
    document's legend** — the documented rule, the same one ``set_palette``
    follows — so an override may name a kind the legend has no glyph for. Two
    further tiers keep the render honest rather than silently drawing the
    authored tile, which is the one thing ``open`` exists to stop:

    1. the glyph :data:`DEFAULT_LEGEND` gives that kind, when the document has
       not spent that character on something else — so a flood reads as ``~``
       the way it does on every generated map;
    2. otherwise the next free :data:`SPARE_GLYPHS` character.

    Either way the result's ``legend`` names what it landed on, like every other
    glyph, so nothing is undecodable and no sixth reserved glyph is invented:
    :data:`RESERVED_GLYPHS` is closed, a legend claiming one is a validation
    error, and widening it would retroactively invalidate documents that legend
    that character today. ``taken`` is whatever else the render will draw —
    the caller's token marks — because a borrowed glyph that collides with one
    is a square the reader cannot resolve.

    A map that has left no character free is refused with the remedy. Refusing
    every such render instead would be worse: the terrain kinds are legal, the
    fight is running on them, and the render is how anyone sees it.
    """
    glyph_of: dict[str, str] = {}
    order_of: dict[str, int] = {}
    for position, (glyph, kind) in enumerate(document.legend.items()):
        if kind not in glyph_of:
            glyph_of[kind] = glyph
            order_of[kind] = position
    if live is None:
        return glyph_of, order_of

    wanted = sorted({live.terrain_at(square) for square in live.claims} - set(glyph_of))
    spent = set(document.legend) | set(RESERVED_GLYPHS) | set(taken)
    for kind in wanted:
        preferred = _FORMAT_GLYPH_OF.get(kind)
        if preferred is not None and preferred not in spent:
            glyph_of[kind] = preferred
            spent.add(preferred)
    spare = (glyph for glyph in SPARE_GLYPHS if glyph not in spent)
    # Ranked after every legend kind, so a downsampled tie still falls to what
    # the document itself names first.
    for position, kind in enumerate(wanted, start=len(document.legend)):
        order_of[kind] = position
        if kind in glyph_of:
            continue
        borrowed = next(spare, None)
        if borrowed is None:
            raise ValueError(
                f"a fixture puts {kind!r} on this map and there is no free glyph "
                f"left to draw it with: this document's legend already spends "
                f"every character a render may borrow. Give {kind!r} a glyph of "
                f"its own with set_legend"
            )
        glyph_of[kind] = borrowed
    return glyph_of, order_of


def _level_or_refuse(document: MapDocument, level: int) -> MapLevel:
    """The named storey, or a refusal listing the ones the map has."""
    if level not in document.levels:
        declared = ", ".join(str(index) for index in sorted(document.levels))
        raise ValueError(f"there is no level {level} on this map. Levels: {declared}")
    return document.levels[level]


def render_ascii(
    document: MapDocument,
    *,
    x: int = 0,
    y: int = 0,
    width: int | None = None,
    height: int | None = None,
    downsample: int = 1,
    show_features: bool = True,
    show_elevation: bool = False,
    level: int = GROUND_LEVEL,
    tokens: Mapping[Square, str] | None = None,
    open: Collection[str] | None = None,
) -> dict[str, Any]:
    """Rows of glyphs for a viewport of the document, through its own legend.

    ``downsample=k`` renders each k-by-k block as its majority kind's glyph,
    ties broken by legend order. Overlays draw over the terrain in fixed
    precedence — tokens, then features, then terrain — using the reserved
    glyphs: ``+`` a closed door, ``/`` an open one, ``<`` and ``>`` stairs,
    ``@`` a spawn hint; ``tokens`` maps squares to single characters (an
    encounter adapter puts combatants here). The viewport is clamped to the
    map, and a result over :data:`RENDER_BUDGET` cells is refused with the
    remedy rather than emitted.

    ``level`` picks the storey to draw; ``levels`` in the result names every one
    the map has, so a reader looking at the ground knows there is more above it.

    ``show_elevation`` adds ``elevation_rows`` and ``elevation_legend`` *beside*
    the terrain rows rather than in place of them — the two layers answer
    different questions and a reader wants both. Heights are lettered from the
    lowest ground in view upward through :data:`HEIGHT_GLYPHS`, so the picture
    reads as a contour; a downsampled block takes its block's majority height,
    ties to the lower ground.

    ``open`` names the fixtures standing open — a fight's live set, straight
    from ``fight.map_state.open_features``. Given one, all three channels
    resolve through it rather than through the file: terrain and ground height
    come from what those fixtures claim (:class:`ResolvedLevel`, shared with
    :func:`query` and the UVTT export), and a fixture's glyph is the state it is
    *in* rather than the state it was authored in. So a sluice a fight has
    opened floods the room its overlay governs, drops that room's ground, and
    draws ``/``.

    Left ``None`` nothing resolves and the render is the map exactly as
    authored, byte for byte as it always was. ``None`` and an empty collection
    are different answers: ``[]`` says every fixture is shut, and shuts one the
    document authored open.

    One consequence worth expecting: ``legend`` reports what the *terrain* layer
    resolved, as it always has, so a door's own square contributes
    ``door-closed`` even though the ``+`` covering it is what the reader sees.
    That entry is true — under the glyph the square really is impassable rather
    than the floor the tiles record — and it is the same square ``show_features``
    turned off would show.
    """
    plane = _level_or_refuse(document, level)
    map_w, map_h = document.grid.width, document.grid.height
    if downsample < 1:
        raise ValueError(f"downsample must be at least 1, got {downsample}")
    x = max(0, min(x, map_w - 1))
    y = max(0, min(y, map_h - 1))
    width = map_w - x if width is None else width
    height = map_h - y if height is None else height
    if width < 1 or height < 1:
        raise ValueError(f"width and height must be at least 1 square, got {width}x{height}")
    width = min(width, map_w - x)
    height = min(height, map_h - y)

    out_w = -(-width // downsample)
    out_h = -(-height // downsample)
    if out_w * out_h > RENDER_BUDGET:
        raise ValueError(
            f"a {width}x{height} viewport at downsample {downsample} is "
            f"{out_w * out_h} cells, over the {RENDER_BUDGET}-cell budget; render a "
            f"smaller viewport (x, y, width, height) or raise downsample"
        )

    marks = dict(tokens or {})
    open_names = None if open is None else frozenset(open)
    live = (
        None if open_names is None
        else ResolvedLevel.of(to_grid(document).levels[level], open_names)
    )
    glyph_of, order_of = _kind_glyphs(document, live, marks.values())

    rows: list[list[str]] = []
    used: dict[str, str] = {}
    heights: list[list[int]] = []
    for r in range(out_h):
        row_out: list[str] = []
        height_row: list[int] = []
        for c in range(out_w):
            x0 = x + c * downsample
            y0 = y + r * downsample
            x1 = min(x0 + downsample, x + width)
            y1 = min(y0 + downsample, y + height)
            counts: dict[str, int] = {}
            feet_counts: dict[int, int] = {}
            for yy in range(y0, y1):
                tile_row = plane.tiles[yy]
                for xx in range(x0, x1):
                    if live is None:
                        kind = document.legend[tile_row[xx]]
                    else:
                        kind = live.terrain_at((xx, yy))
                    counts[kind] = counts.get(kind, 0) + 1
                    if show_elevation:
                        feet = (
                            plane.elevation.at((xx, yy)) if live is None
                            else live.height_at((xx, yy))
                        )
                        feet_counts[feet] = feet_counts.get(feet, 0) + 1
            best = _majority(counts, order_of)
            glyph = glyph_of[best]
            used[glyph] = best
            row_out.append(glyph)
            if show_elevation:
                height_row.append(_lowest_majority(feet_counts))
        rows.append(row_out)
        if show_elevation:
            heights.append(height_row)

    in_view: list[dict[str, Any]] = []
    feature_cells: set[tuple[int, int]] = set()
    for feature in plane.features:
        fx, fy = feature.at
        if not (x <= fx < x + width and y <= fy < y + height):
            continue
        in_view.append(feature_payload(feature))
        if not show_features:
            continue
        # A fixture draws the state it is *in*; one carrying no state has none to
        # be in, and stays the annotation the document drew.
        state = feature.state
        if live is not None and state is not None:
            state = "open" if feature.id in live.open_features else "closed"
        glyph_over = _feature_glyph(feature.kind, state)
        if glyph_over is None:
            continue
        cell = ((fx - x) // downsample, (fy - y) // downsample)
        if cell in feature_cells:
            continue
        feature_cells.add(cell)
        rows[cell[1]][cell[0]] = glyph_over

    token_cells: set[tuple[int, int]] = set()
    for square in sorted(marks):
        sx, sy = square
        if not (x <= sx < x + width and y <= sy < y + height):
            continue
        cell = ((sx - x) // downsample, (sy - y) // downsample)
        if cell in token_cells:
            continue
        token_cells.add(cell)
        rows[cell[1]][cell[0]] = marks[square]

    rendered: dict[str, Any] = {
        "viewport": {
            "x": x, "y": y, "width": width, "height": height, "downsample": downsample,
        },
        "rows": ["".join(row) for row in rows],
        "legend": {glyph: used[glyph] for glyph in sorted(used)},
        "level": level,
        "levels": sorted(document.levels),
        "features_in_view": in_view,
        "truncated": not (x == 0 and y == 0 and width == map_w and height == map_h),
    }
    if show_elevation:
        distinct = sorted({feet for row in heights for feet in row})
        if len(distinct) > len(HEIGHT_GLYPHS):
            raise ValueError(
                f"the viewport holds {len(distinct)} distinct heights, over the "
                f"{len(HEIGHT_GLYPHS)} the height glyphs can name; render a smaller "
                f"viewport (x, y, width, height) or raise downsample"
            )
        glyph_for = {feet: HEIGHT_GLYPHS[index] for index, feet in enumerate(distinct)}
        rendered["elevation_rows"] = [
            "".join(glyph_for[feet] for feet in row) for row in heights
        ]
        rendered["elevation_legend"] = {glyph_for[feet]: feet for feet in distinct}
    return rendered


# --- geometry queries -------------------------------------------------------
_QUERIES = ("distance", "line_of_sight", "path")


def query(
    document: MapDocument,
    kind: str,
    frm: Square,
    to: Square,
    *,
    terrain: TerrainTable,
    rule: DiagonalRule = DiagonalRule.FIVE_FIVE_FIVE,
    level: int = GROUND_LEVEL,
) -> dict[str, Any]:
    """Answer a geometry question over a bare map: distance, sight, or a route.

    Wraps the grid kernel over :func:`~fivee_sim.map_document.to_grid`'s battle
    map, composing step cost and opacity exactly as an encounter does — except
    that with no fight in progress, doors count in their recorded *default*
    state and no square is occupied.

    ``distance`` and ``line_of_sight`` are flat questions and stay flat: height
    reaches the route and nothing else. A ``path`` result therefore carries the
    endpoints' ground heights, because a cost that includes a climb is otherwise
    unexplainable from the answer alone.
    """
    if kind not in _QUERIES:
        raise ValueError(f"unknown query {kind!r}; valid queries: {', '.join(_QUERIES)}")
    _level_or_refuse(document, level)
    map_w, map_h = document.grid.width, document.grid.height
    for label, square in (("from", frm), ("to", to)):
        if not (0 <= square[0] < map_w and 0 <= square[1] < map_h):
            raise ValueError(
                f"{label} is [{square[0]}, {square[1]}], outside the {map_w}x{map_h} map"
            )
    result: dict[str, Any] = {"query": kind, "from": list(frm), "to": list(to)}
    if kind == "distance":
        result["feet"] = distance_feet(square_center(frm), square_center(to), rule)
        return result

    battle = to_grid(document)
    plane = battle.levels[level]
    # A fixture stands in the state the document authored rather than one a
    # MapState overlay has moved since, because there is no fight in progress to
    # have moved it — the one difference from what render_ascii asks of the same
    # derivation. Everything below then mirrors Encounter's composers
    # (_terrain_at, _elevation_at, _opaque) minus the occupancy.
    live = ResolvedLevel.of(
        plane,
        [name for name, feature in plane.features.items() if feature.initially_open],
    )

    def on_map(square: Square) -> bool:
        return 0 <= square[0] < map_w and 0 <= square[1] < map_h

    def step_cost(origin: Square, step_to: Square, doubled_diagonal: bool) -> int | None:
        if not on_map(step_to):
            return None
        return step_cost_feet(
            terrain_effect_of(live.terrain_at(step_to), terrain),
            live.height_at(step_to) - live.height_at(origin),
            doubled_diagonal=doubled_diagonal,
        )

    def opaque(square: Square) -> bool:
        return on_map(square) and terrain_effect_of(
            live.terrain_at(square), terrain
        ).opaque

    if kind == "line_of_sight":
        result["line_of_sight"] = has_line_of_sight(frm, to, opaque=opaque)
        return result

    path = find_path(frm, to, step_cost=step_cost, rule=rule, bounds=(map_w, map_h))
    result["from_elevation"] = live.height_at(frm)
    result["to_elevation"] = live.height_at(to)
    if path is None:
        result["reachable"] = False
        return result
    result["reachable"] = True
    result["squares"] = [[square[0], square[1]] for square in path.squares]
    result["cost_feet"] = path.cost_feet
    return result


# --- files ------------------------------------------------------------------
def environment_roots(env: Mapping[str, str] | None = None) -> list[str]:
    """Map roots the environment asks for, mirroring the content precedence.

    ``FIVEE_SIM_MAPS`` wins outright when set; entries may be files or
    directories. Only when it is unset does the project directory apply.
    """
    environ = os.environ if env is None else env
    configured = environ.get(MAPS_ENV, "").strip()
    if configured:
        return [part for part in configured.split(os.pathsep) if part.strip()]
    project = (
        environ.get(PROJECT_ENV, "").strip()
        or environ.get(CLAUDE_PROJECT_ENV, "").strip()
    )
    if project:
        return [str(Path(project) / MAPS_SUBDIR)]
    return []


def maps_root(env: Mapping[str, str] | None = None) -> Path:
    """Where maps are saved by default: the first configured root, or the
    project's ``.fivee-sim/maps``, or the same under the current directory."""
    roots = environment_roots(env)
    if roots:
        return Path(roots[0]).expanduser()
    return Path.cwd() / MAPS_SUBDIR


def parse_payload(
    payload: Mapping[str, Any], *, source: str, terrain: TerrainTable
) -> tuple[MapDocument, list[Diagnostic]]:
    """Parse one payload, returning the document and any warnings.

    Errors raise :class:`~fivee_sim.map_document.MapError` carrying every
    diagnostic; warnings ride along with a successful parse rather than being
    swallowed.
    """
    diagnostics = validate_document(payload, source=source, terrain=terrain)
    if any(d.severity is Severity.ERROR for d in diagnostics):
        raise MapError(diagnostics)
    document = parse_document(payload, source=source, terrain=terrain)
    return document, [d for d in diagnostics if d.severity is Severity.WARNING]


def load_file(
    path: str | Path, *, terrain: TerrainTable
) -> tuple[MapDocument, list[Diagnostic]]:
    """Read and validate one map file. Errors raise; warnings are returned."""
    file = Path(path).expanduser()
    source = str(file)

    def unreadable(problem: str) -> MapError:
        return MapError([Diagnostic(source=source, section="map", problem=problem)])

    try:
        size = file.stat().st_size
    except OSError as error:
        raise unreadable(f"cannot be read: {error}") from error
    if size > MAX_MAP_BYTES:
        raise unreadable(
            f"is {size} bytes, over the {MAX_MAP_BYTES} byte limit for a map document"
        )
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as error:
        raise unreadable(f"cannot be read: {error}") from error
    except UnicodeDecodeError as error:
        raise unreadable(f"is not valid UTF-8: {error}") from error
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise unreadable(f"is not valid JSON: {error}") from error
    return parse_payload(payload, source=source, terrain=terrain)


def save_file(
    document: MapDocument, path: str | Path, *, overwrite: bool = False
) -> dict[str, Any]:
    """Write the document's canonical text, refusing a silent overwrite."""
    file = Path(path).expanduser()
    if file.exists() and not overwrite:
        raise ValueError(
            f"{file} already exists; pass overwrite=True to replace it deliberately"
        )
    text = serialize(document)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding="utf-8")
    return {
        "path": str(file),
        "bytes": len(text.encode("utf-8")),
        "sha256": sha256_of(text),
    }


def _discover_files(roots: Sequence[str | Path]) -> list[Path]:
    """Every ``*.json`` the roots name, with the content loader's containment
    rule: a named file is taken at its word, a directory refuses symlinks that
    escape it. Unreadable entries are skipped — this feeds a listing, and a
    listing's job is to show what is usable."""
    found: list[Path] = []
    for entry in roots:
        try:
            root = Path(entry).expanduser().resolve()
        except OSError:
            continue
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix.lower() == ".json":
                found.append(root)
            continue
        found.extend(contained_json_files(root))
    return found


def list_maps(roots: Sequence[str | Path] | None = None) -> list[dict[str, Any]]:
    """Every map document under the given (or configured) roots, briefly.

    Reads each file just far enough for a catalogue row — no terrain table is
    needed, so a listing works before any content is loaded. Files that are
    not map documents are skipped, not reported: this is a directory listing,
    not a validator, and :func:`load_file` is where problems get named.
    """
    if roots is None:
        configured = environment_roots()
        roots = configured if configured else [maps_root()]
    listed: list[dict[str, Any]] = []
    for path in _discover_files(roots):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("format") != FORMAT:
            continue
        grid = payload.get("grid")
        provenance = payload.get("provenance")
        if not isinstance(grid, dict) or not isinstance(provenance, dict):
            continue
        listed.append(
            {
                "name": payload.get("name"),
                "path": str(path),
                "width": grid.get("width"),
                "height": grid.get("height"),
                "generator": provenance.get("generator"),
                "edited": provenance.get("edited"),
            }
        )
    listed.sort(key=lambda entry: str(entry["path"]))
    return listed
