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
    "MapError",
    "MapFeatureRecord",
    "MapGrid",
    "MapProvenance",
    "as_payload",
    "document_from",
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
    {"format", "format_version", "name", "grid", "legend", "tiles", "features", "provenance"}
)
_GRID_KEYS = frozenset({"width", "height", "cell_feet"})
_FEATURE_KEYS = frozenset({"id", "kind", "at", "orientation", "state", "team"})
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
class MapFeatureRecord:
    """One feature as the document records it — defaults, not live state."""

    id: str
    kind: str
    at: Square
    orientation: str | None = None
    state: str | None = None
    team: str | None = None


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
    """One parsed, validated map file. Frozen: every edit builds a new one."""

    name: str
    grid: MapGrid
    legend: Mapping[str, str]
    tiles: tuple[str, ...]
    features: tuple[MapFeatureRecord, ...]
    provenance: MapProvenance


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


def _parse_features(
    payload: Mapping[str, Any],
    reader: Reader,
    diagnostics: list[Diagnostic],
    grid: MapGrid | None,
    source: str,
) -> tuple[MapFeatureRecord, ...]:
    raw = payload.get("features", [])
    if not isinstance(raw, list):
        reader.fail("features", "must be a list of feature objects")
        return ()
    features: list[MapFeatureRecord] = []
    claimed: dict[str, int] = {}
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            reader.fail("features", f"feature #{index} must be an object")
            continue
        raw_id = entry.get("id")
        label = raw_id if isinstance(raw_id, str) and raw_id.strip() else f"feature #{index}"
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

        # A door must be fully resolved, like provenance params: reading the
        # document alone must answer how it starts and how it hangs.
        if kind == "door":
            if orientation is None:
                sub.fail("orientation", "required for a door")
            if state is None:
                sub.fail("state", "required for a door; the document stores the default")

        if feature_id.strip():
            if feature_id in claimed:
                sub.fail(
                    "id",
                    f"is already used by feature #{claimed[feature_id]}; ids must be unique",
                )
            else:
                claimed[feature_id] = index
        if sub.ok and at is not None:
            features.append(
                MapFeatureRecord(
                    id=feature_id, kind=kind, at=at,
                    orientation=orientation, state=state, team=team,
                )
            )
    return tuple(features)


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
    features = _parse_features(payload, reader, diagnostics, grid, source)
    provenance = _parse_provenance(payload, reader, diagnostics, source)

    if (
        grid is None
        or legend is None
        or provenance is None
        or any(d.severity is Severity.ERROR for d in diagnostics)
    ):
        return None
    return MapDocument(
        name=name,
        grid=grid,
        legend=MappingProxyType(dict(legend)),
        tiles=tiles,
        features=features,
        provenance=provenance,
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
def as_payload(document: MapDocument) -> dict[str, Any]:
    """The document as JSON-ready primitives, in the canonical key order.

    Legend and params are sorted by key; features keep document order and omit
    the optional fields they do not carry. This is what makes
    :func:`serialize` byte-stable across a parse round-trip.
    """
    features: list[dict[str, Any]] = []
    for feature in document.features:
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
        features.append(entry)
    return {
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
        "features": features,
        "provenance": {
            "generator": document.provenance.generator,
            "seed": document.provenance.seed,
            "params": {
                key: document.provenance.params[key]
                for key in sorted(document.provenance.params)
            },
            "edited": document.provenance.edited,
            "source": document.provenance.source,
        },
    }


def serialize(document: MapDocument) -> str:
    """Canonical text for the document: stable key order, LF, trailing newline.

    parse → serialize → parse is byte-stable, pinned by test, so saving an
    unchanged map rewrites identical bytes and version control stays quiet.
    """
    return json.dumps(as_payload(document), indent=2, ensure_ascii=False) + "\n"


# --- encoding generator output ---------------------------------------------
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
        tiles=tuple(tiles),
        features=tuple(
            MapFeatureRecord(
                id=feature.id, kind=feature.kind, at=feature.at,
                orientation=feature.orientation, state=feature.state, team=feature.team,
            )
            for feature in generated.features
        ),
        provenance=MapProvenance(
            generator=generator, seed=seed, params=MappingProxyType(resolved),
            edited=False, source=GENERATED_SOURCE,
        ),
    )


# --- the bridge to the battle map ------------------------------------------
def to_grid(document: MapDocument) -> BattleMap:
    """The single bridge from a document to an encounter-facing battle map.

    ``default_terrain`` is the most common kind on the tiles (ties broken by
    kind name, so the choice is deterministic); only squares that differ enter
    the sparse mapping. Door features become :class:`MapFeature` rows with
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
        features=features,
        provenance=document.provenance.source,
    )
