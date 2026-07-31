"""Map documents: the file a battle map lives in between fights.

A map document is a single JSON file with a lifecycle — generated, hand-edited,
played — and the file is the source of truth. That is why it is not a fifth
content-pack section: packs are merged named-record registries, and merging two
maps by name is meaningless. It validates under the same idiom as packs, via
the shared :mod:`fivee_sim.validation` machinery: every problem collected,
located diagnostics, unknown keys refused with the valid list.

The format, ``fivee-sim-map`` version 1:

- ``grid`` — width and height in squares (1..``MAX_MAP_DIM``), with
  ``cell_feet`` fixed at 5 so the file says what its numbers mean.
- ``tiles`` — one string per row, top row first, each character resolved
  through the per-document ``legend`` to a terrain-kind string. Opacity is
  tile-based; the format has no edge walls.
- ``elevation`` — optional ground height in feet: a ``default`` and a sparse
  list of ``[x, y, feet]`` for the squares that differ from it. Omitted
  entirely on a flat map, so a file written before heights existed round-trips
  to the same bytes. A reader that predates the key refuses the document as an
  unknown key rather than silently flattening it, which is why the format
  version does not move.
- ``features`` — doors, stairs, spawn hints. A door records its *default*
  open/closed state and an orientation; live state belongs to the encounter
  overlay, never the document. Door cells are ordinary floor in ``tiles`` —
  the feature supplies the blocking.
- ``provenance`` — generator, seed, fully-resolved params (defaults included,
  so the document alone reproduces the map), the ``edited`` flag, and a
  source string. Editing a map flips ``edited`` and leaves the rest alone.

The glyphs ``+`` ``/`` ``<`` ``>`` ``@`` are reserved for renderer overlays
(doors, stairs, spawns); a legend claiming one is a validation error.

:func:`serialize` writes canonical bytes — stable key order, sorted legend and
params, LF line endings, trailing newline — so parse → serialize → parse is
byte-stable and a saved file diffs cleanly. :func:`to_grid` is the single
bridge to the encounter-facing :class:`~fivee_sim.model.battlemap.BattleMap`.
"""

from __future__ import annotations

import dataclasses
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .kernel.grid import FEET_PER_SQUARE, Square, TerrainTable
from .kernel.mapgen import GeneratedMap
from .model.battlemap import BattleMap, MapFeature
from .validation import Diagnostic, Reader, Severity

__all__ = [
    "DEFAULT_LEGEND",
    "FORMAT",
    "FORMAT_VERSION",
    "GENERATED_SOURCE",
    "MAX_MAP_BYTES",
    "MAX_MAP_DIM",
    "RESERVED_GLYPHS",
    "MapDocument",
    "MapElevation",
    "MapError",
    "MapFeatureRecord",
    "MapGrid",
    "MapLevel",
    "MapProvenance",
    "as_payload",
    "document_from",
    "feature_payload",
    "parse_document",
    "serialize",
    "to_grid",
    "validate_document",
]

FORMAT = "fivee-sim-map"
FORMAT_VERSION = 1

#: The provenance ``source`` written for generator output. Original content:
#: generated layouts derive from no published material.
GENERATED_SOURCE = "Generated original content; 5E-compatible"

#: Documents refuse to grow past either cap: a 512-square side keeps every
#: whole-map pass affordable, and the byte cap stops a runaway file from
#: stalling a session before validation can even locate the problem.
MAX_MAP_DIM = 512
MAX_MAP_BYTES = 4 * 1024 * 1024

#: Glyphs the renderers draw *over* the terrain — doors, stairs, spawn marks.
#: A legend may not claim them, or a rendered map would be ambiguous.
RESERVED_GLYPHS = frozenset("+/<>@")

#: The glyph table the generators encode with. A document may define its own;
#: this one is the shared default, and every terrain kind a bundled generator
#: emits has an entry here.
DEFAULT_LEGEND: Mapping[str, str] = MappingProxyType(
    {
        ".": "floor",
        "#": "wall",
        "~": "water",
        ",": "plain",
        "T": "forest",
        "h": "hill",
        "^": "mountain",
        "%": "difficult",
    }
)

_DOCUMENT_KEYS = frozenset(
    {
        "format", "format_version", "name", "grid", "legend", "tiles",
        "elevation", "features", "levels", "provenance",
    }
)
_GRID_KEYS = frozenset({"width", "height", "cell_feet"})
_ELEVATION_KEYS = frozenset({"default", "squares"})
_LEVEL_KEYS = frozenset({"index", "name", "tiles", "elevation", "features"})
_FEATURE_KEYS = frozenset(
    {"id", "kind", "at", "orientation", "state", "team", "to_level"}
)

#: The index of the ground plane. It is the one level the file keeps in its
#: top-level ``tiles``/``elevation``/``features`` keys rather than in ``levels``,
#: so a document with no storeys is byte-identical to one written before floors
#: existed.
GROUND_LEVEL = 0
_PROVENANCE_KEYS = frozenset({"generator", "seed", "params", "edited", "source"})
_DOOR_ORIENTATIONS = ("horizontal", "vertical")
_DOOR_STATES = ("open", "closed")


class MapError(ValueError):
    """A map document is invalid. Carries every diagnostic, not the first."""

    def __init__(self, diagnostics: list[Diagnostic] | tuple[Diagnostic, ...]) -> None:
        self.diagnostics = tuple(diagnostics)
        errors = [d for d in self.diagnostics if d.severity is Severity.ERROR]
        super().__init__(
            f"{len(errors)} map error(s):\n" + "\n".join(f"  {d.describe()}" for d in errors)
        )


@dataclass(frozen=True, slots=True)
class MapGrid:
    """The document's dimensions, in squares, with the cell size spelt out."""

    width: int
    height: int
    cell_feet: int = FEET_PER_SQUARE


@dataclass(frozen=True, slots=True)
class MapElevation:
    """Ground height in feet: a default, and the squares that differ from it.

    Heights are plain feet and may be negative — a pit floor is below the datum
    the rest of the map sits on. The default instance is a flat map at zero, and
    it is the one shape :func:`as_payload` leaves out of the document entirely.
    """

    default: int = 0
    squares: Mapping[Square, int] = dataclasses.field(default_factory=dict)

    def at(self, square: Square) -> int:
        return self.squares.get(square, self.default)


@dataclass(frozen=True, slots=True)
class MapFeatureRecord:
    """One feature as the document records it — defaults, not live state.

    ``to_level`` is what makes a stairway more than a drawn glyph: it names the
    level the feature leads to, and the square it lands on is the one it stands
    on. A feature without it is an ordinary fixture that goes nowhere.
    """

    id: str
    kind: str
    at: Square
    orientation: str | None = None
    state: str | None = None
    team: str | None = None
    to_level: int | None = None


@dataclass(frozen=True, slots=True)
class MapLevel:
    """One storey: a full plane of tiles, heights, and fixtures over the grid.

    Every level shares the document's ``grid`` and ``legend`` — floors of one
    building, not unrelated maps — so only what differs between them lives here.
    ``elevation.default`` is the level's own datum, which is how a first floor
    sits ten feet above the ground one without a second concept for it.
    """

    index: int
    name: str
    tiles: tuple[str, ...]
    features: tuple[MapFeatureRecord, ...]
    elevation: MapElevation = dataclasses.field(default_factory=MapElevation)


@dataclass(frozen=True, slots=True)
class MapProvenance:
    """Where the map came from, completely enough to regenerate it.

    ``params`` is fully resolved — defaults included — so the document alone
    reproduces the map; ``edited`` records that a human or a tool has touched
    the tiles since, at which point the file, not the generator, is the truth.
    """

    generator: str
    seed: int
    params: Mapping[str, Any]
    edited: bool
    source: str


@dataclass(frozen=True, slots=True)
class MapDocument:
    """One parsed, validated map file. Frozen: every edit builds a new one.

    ``levels`` always holds :data:`GROUND_LEVEL`, and holds only that for a map
    with no storeys. The ground is reachable as :attr:`ground`, and the three
    accessors below read it, because that is what a caller asking a map for its
    tiles has always meant — the storeys are the addition, never a repointing.
    """

    name: str
    grid: MapGrid
    legend: Mapping[str, str]
    provenance: MapProvenance
    levels: Mapping[int, MapLevel]

    @property
    def ground(self) -> MapLevel:
        return self.levels[GROUND_LEVEL]

    @property
    def tiles(self) -> tuple[str, ...]:
        return self.ground.tiles

    @property
    def features(self) -> tuple[MapFeatureRecord, ...]:
        return self.ground.features

    @property
    def elevation(self) -> MapElevation:
        return self.ground.elevation


# --- parsing ---------------------------------------------------------------
def _parse_grid(
    payload: Mapping[str, Any], reader: Reader, diagnostics: list[Diagnostic], source: str
) -> MapGrid | None:
    raw = payload.get("grid")
    if raw is None:
        reader.fail("grid", "required")
        return None
    if not isinstance(raw, Mapping):
        reader.fail("grid", f"must be an object, got {type(raw).__name__}")
        return None
    sub = Reader(raw, diagnostics, source=source, section="map", name="grid")
    sub.unknown_keys(_GRID_KEYS)
    width = sub.integer("width", required=True, minimum=1, default=1)
    height = sub.integer("height", required=True, minimum=1, default=1)
    for label, value in (("width", width), ("height", height)):
        if value > MAX_MAP_DIM:
            sub.fail(label, f"must be at most {MAX_MAP_DIM} squares, got {value}")
    cell_feet = sub.integer("cell_feet", required=True, default=FEET_PER_SQUARE)
    if cell_feet != FEET_PER_SQUARE:
        sub.fail(
            "cell_feet",
            f"must be {FEET_PER_SQUARE}; this format is defined on 5-foot squares",
        )
    if not sub.ok:
        return None
    return MapGrid(width=width, height=height, cell_feet=cell_feet)


def _parse_legend(
    payload: Mapping[str, Any],
    reader: Reader,
    *,
    terrain: TerrainTable,
) -> dict[str, str] | None:
    """The legend as a plain dict, or ``None`` when it was not a usable mapping.

    Entries that fail a *value* check (reserved glyph, unknown kind) still land
    in the returned dict: the error is already reported once, and leaving the
    glyph resolvable stops every tile that uses it from re-reporting it.
    """
    raw = payload.get("legend")
    if raw is None:
        reader.fail("legend", "required")
        return None
    if not isinstance(raw, Mapping):
        reader.fail(
            "legend",
            "must be an object mapping single characters to terrain kinds, "
            'such as {".": "floor", "#": "wall"}',
        )
        return None
    available = ", ".join(sorted(terrain)) or "none"
    legend: dict[str, str] = {}
    for glyph in sorted(raw, key=str):
        kind = raw[glyph]
        if not isinstance(glyph, str) or len(glyph) != 1:
            reader.fail("legend", f"glyph {glyph!r} must be a single character")
            continue
        if not isinstance(kind, str) or not kind.strip():
            reader.fail("legend", f"glyph {glyph!r} must name a terrain kind, got {kind!r}")
            continue
        if glyph in RESERVED_GLYPHS:
            reader.fail(
                "legend",
                f"glyph {glyph!r} is reserved for renderer overlays "
                f"({' '.join(sorted(RESERVED_GLYPHS))}) and cannot name terrain",
            )
        if kind not in terrain:
            reader.fail(
                "legend",
                f"glyph {glyph!r} names terrain {kind!r}, which the active content "
                f"does not define. Available: {available}",
            )
        legend[glyph] = kind
    return legend


def _parse_tiles(
    payload: Mapping[str, Any],
    reader: Reader,
    grid: MapGrid | None,
    legend: dict[str, str] | None,
) -> tuple[str, ...]:
    raw = payload.get("tiles")
    if raw is None:
        reader.fail("tiles", "required")
        return ()
    if not isinstance(raw, list) or not all(isinstance(row, str) for row in raw):
        reader.fail("tiles", "must be a list of strings, one per map row, top row first")
        return ()
    tiles = tuple(raw)
    if grid is not None:
        if len(tiles) != grid.height:
            reader.fail("tiles", f"has {len(tiles)} rows; the grid is {grid.height} squares high")
        for y, row in enumerate(tiles):
            if len(row) != grid.width:
                reader.fail(
                    "tiles",
                    f"row {y} is {len(row)} characters; the grid is {grid.width} squares wide",
                )
    if legend is not None:
        # Each unknown glyph is reported once, at its first appearance — a map
        # whose legend dropped a glyph should not produce a thousand copies of
        # the same message.
        unknown: dict[str, Square] = {}
        for y, row in enumerate(tiles):
            for x, char in enumerate(row):
                if char not in legend and char not in unknown:
                    unknown[char] = (x, y)
        for char in sorted(unknown):
            x, y = unknown[char]
            reader.fail(
                "tiles",
                f"row {y} column {x} uses {char!r}, which the legend does not define",
            )
    return tiles


def _parse_elevation(
    payload: Mapping[str, Any],
    reader: Reader,
    diagnostics: list[Diagnostic],
    grid: MapGrid | None,
    source: str,
) -> MapElevation | None:
    """The height layer, or the flat default when the document does not carry one.

    ``None`` only when the key is present and unusable; an absent key is a flat
    map, which is what every document written before heights existed is.
    """
    raw = payload.get("elevation")
    if raw is None:
        return MapElevation()
    if not isinstance(raw, Mapping):
        reader.fail(
            "elevation",
            'must be an object with a "default" height in feet and a "squares" '
            'list of [x, y, feet], such as {"default": 0, "squares": [[3, 4, 20]]}',
        )
        return None
    sub = Reader(raw, diagnostics, source=source, section="map", name="elevation")
    sub.unknown_keys(_ELEVATION_KEYS)
    default = sub.integer("default")
    entries = raw.get("squares", [])
    if not isinstance(entries, list):
        sub.fail("squares", "must be a list of [x, y, feet] entries")
        return None
    squares: dict[Square, int] = {}
    for index, entry in enumerate(entries):
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) != 3
            or any(isinstance(value, bool) or not isinstance(value, int) for value in entry)
        ):
            sub.fail("squares", f"entry #{index} must be [x, y, feet], got {entry!r}")
            continue
        square = (int(entry[0]), int(entry[1]))
        if grid is not None and not (
            0 <= square[0] < grid.width and 0 <= square[1] < grid.height
        ):
            sub.fail(
                "squares",
                f"entry #{index} is at ({square[0]}, {square[1]}), outside the "
                f"{grid.width}x{grid.height} grid",
            )
            continue
        if square in squares:
            sub.fail(
                "squares",
                f"entry #{index} names square ({square[0]}, {square[1]}) again; "
                f"it is already {squares[square]} ft",
            )
            continue
        squares[square] = int(entry[2])
    if not sub.ok:
        return None
    return MapElevation(default=default, squares=MappingProxyType(squares))


def _parse_features(
    raw: Any,
    reader: Reader,
    diagnostics: list[Diagnostic],
    grid: MapGrid | None,
    source: str,
    *,
    claimed: dict[str, str],
    where: str = "",
) -> tuple[MapFeatureRecord, ...]:
    """One level's features. ``claimed`` spans the document, ``where`` locates it.

    Ids are unique across every level, not within one: the battle map keys its
    features by name in a single table, so two storeys sharing an id would
    resolve to one feature rather than two. Doors, by contrast, collide only
    within a level — two floors may each hang one over the same square.
    """
    if not isinstance(raw, list):
        reader.fail("features", "must be a list of feature objects")
        return ()
    features: list[MapFeatureRecord] = []
    door_squares: dict[Square, str] = {}
    for index, entry in enumerate(raw):
        position = f"feature #{index}{where}"
        if not isinstance(entry, Mapping):
            reader.fail("features", f"{position} must be an object")
            continue
        raw_id = entry.get("id")
        label = raw_id if isinstance(raw_id, str) and raw_id.strip() else position
        sub = Reader(entry, diagnostics, source=source, section="map", name=label)
        sub.unknown_keys(_FEATURE_KEYS)
        feature_id = sub.string("id", required=True)
        if isinstance(raw_id, str) and not raw_id.strip():
            sub.fail("id", "must be non-empty text")
        kind = sub.string("kind", required=True)
        if isinstance(entry.get("kind"), str) and not kind.strip():
            sub.fail("kind", "must be non-empty text")

        at: Square | None = None
        raw_at = entry.get("at")
        if raw_at is None:
            sub.fail("at", "required")
        elif (
            not isinstance(raw_at, (list, tuple))
            or len(raw_at) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in raw_at)
        ):
            sub.fail("at", f"must be [x, y] square indices, got {raw_at!r}")
        else:
            at = (int(raw_at[0]), int(raw_at[1]))
            if grid is not None and not (
                0 <= at[0] < grid.width and 0 <= at[1] < grid.height
            ):
                sub.fail(
                    "at",
                    f"({at[0]}, {at[1]}) is outside the {grid.width}x{grid.height} grid",
                )

        orientation = sub.string("orientation") or None
        if orientation is not None and orientation not in _DOOR_ORIENTATIONS:
            sub.fail(
                "orientation",
                f"must be one of: {', '.join(_DOOR_ORIENTATIONS)}; got {orientation!r}",
            )
        state = sub.string("state") or None
        if state is not None and state not in _DOOR_STATES:
            sub.fail("state", f"must be one of: {', '.join(_DOOR_STATES)}; got {state!r}")
        team = sub.string("team") or None

        to_level: int | None = None
        if "to_level" in entry:
            raw_target = entry["to_level"]
            if isinstance(raw_target, bool) or not isinstance(raw_target, int):
                sub.fail(
                    "to_level",
                    f"must name a level by whole number, got {raw_target!r}",
                )
            else:
                to_level = int(raw_target)

        # A door must be fully resolved, like provenance params: reading the
        # document alone must answer how it starts and how it hangs.
        if kind == "door":
            if orientation is None:
                sub.fail("orientation", "required for a door")
            if state is None:
                sub.fail("state", "required for a door; the document stores the default")
            # One door per square: the encounter refuses a map whose doors
            # collide, so the document must refuse it first — a battle map
            # resolves a square to one feature state, never two. Annotations
            # (stairs, spawns) may share squares freely.
            if at is not None:
                if at in door_squares:
                    sub.fail(
                        "at",
                        f"({at[0]}, {at[1]}) already holds door "
                        f"'{door_squares[at]}'; one door per square",
                    )
                else:
                    door_squares[at] = label

        if feature_id.strip():
            if feature_id in claimed:
                sub.fail(
                    "id",
                    f"is already used by {claimed[feature_id]}; ids must be unique",
                )
            else:
                claimed[feature_id] = position
        if sub.ok and at is not None:
            features.append(
                MapFeatureRecord(
                    id=feature_id, kind=kind, at=at,
                    orientation=orientation, state=state, team=team, to_level=to_level,
                )
            )
    return tuple(features)


def _parse_levels(
    payload: Mapping[str, Any],
    reader: Reader,
    diagnostics: list[Diagnostic],
    grid: MapGrid | None,
    legend: dict[str, str] | None,
    source: str,
    claimed: dict[str, str],
) -> dict[int, MapLevel]:
    """The storeys above and below the ground, keyed by index.

    The ground is not among them — it lives in the document's own
    ``tiles``/``elevation``/``features`` — which is why
    :data:`GROUND_LEVEL` is refused here rather than accepted as a second
    spelling of it.
    """
    raw = payload.get("levels")
    if raw is None:
        return {}
    if not isinstance(raw, list):
        reader.fail(
            "levels",
            "must be a list of level objects, each with an index and its own tiles",
        )
        return {}
    levels: dict[int, MapLevel] = {}
    for position, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            reader.fail("levels", f"level #{position} must be an object")
            continue
        raw_index = entry.get("index")
        label = f"level {raw_index}" if isinstance(raw_index, int) else f"level #{position}"
        sub = Reader(entry, diagnostics, source=source, section="map", name=label)
        sub.unknown_keys(_LEVEL_KEYS)
        if raw_index is None:
            sub.fail("index", "required: the storey's height order, above or below the ground")
            continue
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            sub.fail("index", f"must be a whole number, got {raw_index!r}")
            continue
        index = int(raw_index)
        if index == GROUND_LEVEL:
            sub.fail(
                "index",
                f"{GROUND_LEVEL} is the ground plane, which lives in the document's own "
                f"tiles, elevation, and features; a storey needs another index",
            )
            continue
        if index in levels:
            sub.fail("index", "is already declared by an earlier level")
            continue
        tiles = _parse_tiles(entry, sub, grid, legend)
        elevation = _parse_elevation(entry, sub, diagnostics, grid, source)
        features = _parse_features(
            entry.get("features", []), sub, diagnostics, grid, source,
            claimed=claimed, where=f" on level {index}",
        )
        levels[index] = MapLevel(
            index=index,
            name=sub.string("name") or f"level {index}",
            tiles=tiles,
            features=features,
            elevation=elevation if elevation is not None else MapElevation(),
        )
    return levels


def _check_connectors(
    document_levels: Mapping[int, MapLevel],
    reader: Reader,
) -> None:
    """Every ``to_level`` names a level that exists, and never its own.

    Deferred to a second pass because a connector on the ground may lead to a
    storey the parser has not read yet.
    """
    for index in sorted(document_levels):
        for feature in document_levels[index].features:
            if feature.to_level is None:
                continue
            if feature.to_level == index:
                reader.fail(
                    "features",
                    f"feature '{feature.id}' leads to its own level ({index}); "
                    f"a connector joins two different levels",
                )
            elif feature.to_level not in document_levels:
                available = ", ".join(str(i) for i in sorted(document_levels))
                reader.fail(
                    "features",
                    f"feature '{feature.id}' leads to level {feature.to_level}, but "
                    f"there is no level {feature.to_level} in this map. Declared: {available}",
                )


def _parse_provenance(
    payload: Mapping[str, Any], reader: Reader, diagnostics: list[Diagnostic], source: str
) -> MapProvenance | None:
    raw = payload.get("provenance")
    if raw is None:
        reader.fail(
            "provenance",
            "required: generator, seed, params, edited, and source — an edited "
            "map must still say where it came from",
        )
        return None
    if not isinstance(raw, Mapping):
        reader.fail("provenance", f"must be an object, got {type(raw).__name__}")
        return None
    sub = Reader(raw, diagnostics, source=source, section="map", name="provenance")
    sub.unknown_keys(_PROVENANCE_KEYS)
    generator = sub.string("generator", required=True)
    seed = sub.integer("seed", required=True)
    raw_params = raw.get("params")
    params: Mapping[str, Any] | None = None
    if raw_params is None:
        sub.fail("params", "required; record the fully-resolved generator parameters")
    elif not isinstance(raw_params, Mapping):
        sub.fail("params", f"must be an object, got {type(raw_params).__name__}")
    else:
        params = raw_params
    if "edited" not in raw:
        sub.fail("edited", "required")
    edited = sub.boolean("edited")
    source_text = sub.string("source", required=True)
    if not sub.ok or params is None:
        return None
    return MapProvenance(
        generator=generator, seed=seed,
        params=MappingProxyType(dict(params)), edited=edited, source=source_text,
    )


def _parse(
    payload: Mapping[str, Any],
    diagnostics: list[Diagnostic],
    *,
    source: str,
    terrain: TerrainTable,
) -> MapDocument | None:
    if not isinstance(payload, Mapping):
        diagnostics.append(
            Diagnostic(source=source, section="map", problem="a map document must be a JSON object")
        )
        return None
    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if size > MAX_MAP_BYTES:
        diagnostics.append(
            Diagnostic(
                source=source, section="map",
                problem=(
                    f"is {size} bytes, over the {MAX_MAP_BYTES} byte limit for a map document"
                ),
            )
        )
        return None

    reader = Reader(payload, diagnostics, source=source, section="map", name="")
    reader.unknown_keys(_DOCUMENT_KEYS)
    declared = reader.string("format", required=True, default=FORMAT)
    if declared != FORMAT:
        reader.fail("format", f'must be "{FORMAT}", got {declared!r}')
    version = reader.integer("format_version", required=True, default=FORMAT_VERSION)
    if version != FORMAT_VERSION:
        reader.fail("format_version", f"must be {FORMAT_VERSION}, got {version}")
    name = reader.string("name", required=True)
    if isinstance(payload.get("name"), str) and not name.strip():
        reader.fail("name", "must be non-empty text")

    grid = _parse_grid(payload, reader, diagnostics, source)
    legend = _parse_legend(payload, reader, terrain=terrain)
    tiles = _parse_tiles(payload, reader, grid, legend)
    elevation = _parse_elevation(payload, reader, diagnostics, grid, source)
    claimed: dict[str, str] = {}
    features = _parse_features(
        payload.get("features", []), reader, diagnostics, grid, source, claimed=claimed
    )
    levels = _parse_levels(payload, reader, diagnostics, grid, legend, source, claimed)
    levels[GROUND_LEVEL] = MapLevel(
        index=GROUND_LEVEL,
        name="ground",
        tiles=tiles,
        features=features,
        elevation=elevation if elevation is not None else MapElevation(),
    )
    _check_connectors(levels, reader)
    provenance = _parse_provenance(payload, reader, diagnostics, source)

    if (
        grid is None
        or legend is None
        or elevation is None
        or provenance is None
        or any(d.severity is Severity.ERROR for d in diagnostics)
    ):
        return None
    return MapDocument(
        name=name,
        grid=grid,
        legend=MappingProxyType(dict(legend)),
        provenance=provenance,
        levels=MappingProxyType(levels),
    )


def parse_document(
    payload: Mapping[str, Any], *, source: str, terrain: TerrainTable
) -> MapDocument:
    """Parse and validate one map document, or raise with every diagnostic.

    ``terrain`` is the active terrain table — passed in, never read from module
    state, so a pack-defined kind validates and an unknown one is refused with
    the loaded list, exactly as conditions work.
    """
    diagnostics: list[Diagnostic] = []
    document = _parse(payload, diagnostics, source=source, terrain=terrain)
    if document is None:
        raise MapError(diagnostics)
    return document


def validate_document(
    payload: Mapping[str, Any], *, source: str, terrain: TerrainTable
) -> list[Diagnostic]:
    """Every problem with the document, without raising. The authoring aid."""
    diagnostics: list[Diagnostic] = []
    _parse(payload, diagnostics, source=source, terrain=terrain)
    return diagnostics


# --- serialization ---------------------------------------------------------
def feature_payload(feature: MapFeatureRecord) -> dict[str, Any]:
    """One feature as JSON-ready primitives, omitting the fields it does not carry.

    The omission is what makes :func:`serialize` byte-stable across a parse
    round-trip, so this shape is the document's, not a caller's convenience —
    the render overlay in :mod:`fivee_sim.service.maps` reports features through
    it too, and a second copy would be free to drift from the written form.
    """
    entry: dict[str, Any] = {
        "id": feature.id,
        "kind": feature.kind,
        "at": [feature.at[0], feature.at[1]],
    }
    if feature.orientation is not None:
        entry["orientation"] = feature.orientation
    if feature.state is not None:
        entry["state"] = feature.state
    if feature.team is not None:
        entry["team"] = feature.team
    if feature.to_level is not None:
        entry["to_level"] = feature.to_level
    return entry


def _elevation_payload(elevation: MapElevation) -> dict[str, Any] | None:
    """The canonical height block, or ``None`` for a flat plane at zero.

    Squares already sitting at the datum are dropped and the rest sorted by row
    then column, so painting a height and painting it back writes the bytes it
    started with.
    """
    raised = {
        square: feet for square, feet in elevation.squares.items() if feet != elevation.default
    }
    if not raised and not elevation.default:
        return None
    return {
        "default": elevation.default,
        "squares": [
            [square[0], square[1], raised[square]]
            for square in sorted(raised, key=lambda s: (s[1], s[0]))
        ],
    }


def _level_payload(level: MapLevel) -> dict[str, Any]:
    """One storey as JSON-ready primitives. The ground never comes through here."""
    entry: dict[str, Any] = {
        "index": level.index,
        "name": level.name,
        "tiles": list(level.tiles),
    }
    elevation = _elevation_payload(level.elevation)
    if elevation is not None:
        entry["elevation"] = elevation
    entry["features"] = [feature_payload(feature) for feature in level.features]
    return entry


def as_payload(document: MapDocument) -> dict[str, Any]:
    """The document as JSON-ready primitives, in the canonical key order.

    Legend and params are sorted by key; features keep document order and omit
    the optional fields they do not carry. Elevation is canonicalised — squares
    already sitting at the default are dropped, the rest sorted by row then
    column — and the whole key is omitted from a flat map at zero, so a document
    written before heights existed writes back byte-for-byte. This is what makes
    :func:`serialize` byte-stable across a parse round-trip.

    The ground plane writes to the top-level ``tiles``/``elevation``/``features``
    keys and the storeys to ``levels``, sorted by index. A map with no storeys
    writes no ``levels`` key at all, which is what keeps a document written
    before floors existed byte-identical too.
    """
    payload: dict[str, Any] = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "name": document.name,
        "grid": {
            "width": document.grid.width,
            "height": document.grid.height,
            "cell_feet": document.grid.cell_feet,
        },
        "legend": {glyph: document.legend[glyph] for glyph in sorted(document.legend)},
        "tiles": list(document.tiles),
    }
    elevation = _elevation_payload(document.elevation)
    if elevation is not None:
        payload["elevation"] = elevation
    payload["features"] = [feature_payload(feature) for feature in document.features]
    storeys = [
        _level_payload(document.levels[index])
        for index in sorted(document.levels)
        if index != GROUND_LEVEL
    ]
    if storeys:
        payload["levels"] = storeys
    payload["provenance"] = {
        "generator": document.provenance.generator,
        "seed": document.provenance.seed,
        "params": {
            key: document.provenance.params[key]
            for key in sorted(document.provenance.params)
        },
        "edited": document.provenance.edited,
        "source": document.provenance.source,
    }
    return payload


def serialize(document: MapDocument) -> str:
    """Canonical text for the document: stable key order, LF, trailing newline.

    parse → serialize → parse is byte-stable, pinned by test, so saving an
    unchanged map rewrites identical bytes and version control stays quiet.
    """
    return json.dumps(as_payload(document), indent=2, ensure_ascii=False) + "\n"


# --- encoding generator output ---------------------------------------------
def _elevation_from(generated: GeneratedMap, name: str) -> MapElevation:
    """A generator's dense height grid as the document's datum-plus-sparse form.

    A generator that emits no heights at all gets the flat default, which
    :func:`as_payload` then omits from the file entirely.
    """
    if not generated.elevation:
        return MapElevation()
    if len(generated.elevation) != generated.height or any(
        len(row) != generated.width for row in generated.elevation
    ):
        raise MapError(
            [
                Diagnostic(
                    source=name, section="map", field="elevation",
                    problem=(
                        f"the height grid is not {generated.width}x{generated.height}; "
                        f"a generator emits one height per cell or none at all"
                    ),
                )
            ]
        )
    counts: Counter[int] = Counter(feet for row in generated.elevation for feet in row)
    datum = min(counts, key=lambda feet: (-counts[feet], feet))
    return MapElevation(
        default=datum,
        squares={
            (x, y): feet
            for y, row in enumerate(generated.elevation)
            for x, feet in enumerate(row)
            if feet != datum
        },
    )


def document_from(
    generated: GeneratedMap, *, name: str, generator: str, seed: int, params: Any
) -> MapDocument:
    """Encode a generator's output as a map document.

    Tiles are written through :data:`DEFAULT_LEGEND` — every kind a bundled
    generator emits has a glyph there, and a kind without one is refused
    rather than assigned an invented glyph, because a silently extended
    legend would make two runs of the same generator disagree. ``params`` is
    the generator's params dataclass (or an equivalent mapping), recorded
    fully resolved so the document alone reproduces the map; ``edited``
    starts false and ``source`` says the content is generated and original.

    A generator's dense height grid is reduced here rather than in the
    generator: the commonest height becomes the document's datum and only the
    squares departing from it are written, which is the same choice
    :func:`to_grid` makes for terrain and for the same reason — the file stays
    small and a run of flat ground costs nothing to record.
    """
    glyph_of = {kind: glyph for glyph, kind in DEFAULT_LEGEND.items()}
    tiles: list[str] = []
    for y, row in enumerate(generated.cells):
        glyphs: list[str] = []
        for x, kind in enumerate(row):
            glyph = glyph_of.get(kind)
            if glyph is None:
                raise MapError(
                    [
                        Diagnostic(
                            source=name, section="map", field="tiles",
                            problem=(
                                f"cell ({x}, {y}) is {kind!r}, which has no glyph in the "
                                f"default legend; add one before encoding"
                            ),
                        )
                    ]
                )
            glyphs.append(glyph)
        tiles.append("".join(glyphs))
    if dataclasses.is_dataclass(params) and not isinstance(params, type):
        resolved: dict[str, Any] = dataclasses.asdict(params)
    else:
        resolved = dict(params)
    return MapDocument(
        name=name,
        grid=MapGrid(width=generated.width, height=generated.height),
        legend=DEFAULT_LEGEND,
        provenance=MapProvenance(
            generator=generator, seed=seed, params=MappingProxyType(resolved),
            edited=False, source=GENERATED_SOURCE,
        ),
        levels=MappingProxyType(
            {
                GROUND_LEVEL: MapLevel(
                    index=GROUND_LEVEL,
                    name="ground",
                    tiles=tuple(tiles),
                    elevation=_elevation_from(generated, name),
                    features=tuple(
                        MapFeatureRecord(
                            id=feature.id, kind=feature.kind, at=feature.at,
                            orientation=feature.orientation, state=feature.state,
                            team=feature.team,
                        )
                        for feature in generated.features
                    ),
                )
            }
        ),
    )


# --- the bridge to the battle map ------------------------------------------
def to_grid(document: MapDocument) -> BattleMap:
    """The single bridge from a document to an encounter-facing battle map.

    ``default_terrain`` is the most common kind on the tiles (ties broken by
    kind name, so the choice is deterministic); only squares that differ enter
    the sparse mapping. Ground height crosses as the document already holds it —
    the author's default and the squares that depart from it — since there is
    nothing to infer. Door features become :class:`MapFeature` rows with
    ``initially_open`` read from the recorded default state. Non-door features
    — stairs, spawn hints — stay document-level *on purpose*: the battle map
    has no slot for them and a fight does not consult them; renderers and
    placement logic read them from the document.
    """
    counts: Counter[str] = Counter()
    for row in document.tiles:
        for char in row:
            counts[document.legend[char]] += 1
    if counts:
        default = min(counts, key=lambda kind: (-counts[kind], kind))
    else:  # pragma: no cover - dimensions are validated to at least 1x1
        default = "floor"

    terrain: dict[Square, str] = {}
    for y, row in enumerate(document.tiles):
        for x, char in enumerate(row):
            kind = document.legend[char]
            if kind != default:
                terrain[(x, y)] = kind

    features: dict[str, MapFeature] = {}
    for feature in document.features:
        if feature.kind != "door":
            continue
        features[feature.id] = MapFeature(
            name=feature.id,
            square=feature.at,
            kind="door",
            initially_open=feature.state == "open",
        )
    return BattleMap(
        name=document.name,
        width=document.grid.width,
        height=document.grid.height,
        default_terrain=default,
        terrain=terrain,
        default_elevation=document.elevation.default,
        elevation=dict(document.elevation.squares),
        features=features,
        provenance=document.provenance.source,
    )
