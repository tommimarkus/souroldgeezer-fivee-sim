"""Universal VTT export: one map document as one importable ``.uvtt`` payload.

Implemented defensively from the publicly documented shape of the format — the
JSON keys an importer reads — with no code ported from any tool that produces
or consumes it. Everything here is stdlib: ``zlib``, ``struct``, ``base64``,
``colorsys``.

What travels, and what deliberately does not:

- ``line_of_sight`` — wall polylines in grid-square units, **derived from the
  terrain**: the document has no edge walls, so every interior cell-side where
  an opaque square meets a non-opaque one becomes a unit edge, and the edges
  are chained and merged into polylines. A door's square is the tile under it
  whatever ``open`` says (the door is a feature, and travels as a portal), so
  wall runs break at doorways by construction and the ``portals`` fill the
  gaps. The map boundary emits nothing: out of bounds counts as opaque, so a
  wall run along the border contributes only its interior-facing edge.
- ``portals`` — one per door feature, spanning its square along the door's
  orientation, ``closed`` from the recorded default state or from ``open``.
- ``image`` — a base64 PNG: flat truecolor fill per terrain kind plus a
  one-pixel grid line, because some importers refuse a file without an image.
  A kind the *document* colors is exported in that color — its ``light`` one,
  since this file has exactly one theme. Everything else is engine policy,
  defined here; it happens to follow the editor renderer's light-theme colors
  for familiarity, but that parity is cosmetic, not contractual — the renderer
  themes at draw time. Pack-defined kinds with no authored color get the same
  deterministic hue-hash fallback formula the renderer uses, so an unknown kind
  is the same color in every export.
- ``lights`` and ``objects_line_of_sight`` ship empty, and elevation does not
  exist here: the engine models none of them, and inventing values would
  misrepresent the map.

``open`` names the fixtures standing open — a fight's live set. Given one, the
walls and the image are derived from what those fixtures *make* of each square
rather than from the tiles, and the portals report the state they are in: a
raised portcullis stops being a wall, a sluice gate's flooded room exports as
water. Left ``None`` nothing resolves and the export is the map exactly as
authored, byte for byte as it always was — ``None`` and ``[]`` are different
answers, and ``[]`` shuts a fixture the document authored open.

An overland map typically has no opaque kinds at all, so it exports with an
image and an empty ``line_of_sight`` — that is correct, not a bug.

Transport-neutral, like the rest of the service layer: plain inputs, plain
:class:`ValueError`, JSON-ready primitives out.
"""

from __future__ import annotations

import base64
import colorsys
import struct
import zlib
from collections.abc import Collection, Mapping
from typing import Any

from ..kernel.grid import TerrainTable, terrain_effect_of
from ..map_document import GROUND_LEVEL, MapColor, MapDocument, MapLevel, to_grid
from .maps import ResolvedLevel, linked_open_features

__all__ = ["MAX_IMAGE_SIDE", "UVTT_FORMAT", "to_uvtt"]

#: The format version the payload declares. A float, because the field is one.
UVTT_FORMAT = 0.3

#: The rendered image refuses to exceed this many pixels on a side. Every
#: valid document (dimensions capped at 512 squares) fits at 8 pixels per
#: square or fewer.
MAX_IMAGE_SIDE = 4096

#: Flat fill colors for the bundled terrain kinds. Engine policy — see the
#: module docstring for why parity with the JS renderer is not contractual.
PALETTE: dict[str, tuple[int, int, int]] = {
    "normal": (0xE9, 0xE4, 0xD8),
    "floor": (0xE9, 0xE4, 0xD8),
    "wall": (0x4D, 0x46, 0x3C),
    "difficult": (0xDC, 0xD3, 0xBD),
    "half-cover": (0xDD, 0xD8, 0xC6),
    "three-quarters-cover": (0xD3, 0xCD, 0xB8),
    "door-open": (0xE2, 0xD3, 0xB4),
    "door-closed": (0x8A, 0x6F, 0x4D),
    "water": (0xA9, 0xC6, 0xCE),
    "plain": (0xD9, 0xDF, 0xB6),
    "forest": (0xA7, 0xC3, 0x96),
    "hill": (0xCF, 0xC4, 0x9C),
    "mountain": (0xB3, 0xAA, 0x9D),
}

#: The one-pixel grid line between cells.
GRID_RGB: tuple[int, int, int] = (0x6B, 0x65, 0x5C)

#: Cells narrower than this get no grid line — it would eat the terrain.
_MIN_GRID_PPG = 4


def _hash_of(text: str) -> int:
    """The renderer's string hash: ``h = h * 31 + code`` over a uint32.

    Reimplemented from the documented formula in ``renderer.js`` so a
    pack-defined kind lands on the same hue in the PNG as on the canvas.
    """
    value = 0
    for char in text:
        value = (value * 31 + ord(char)) & 0xFFFFFFFF
    return value


def _fallback_rgb(kind: str) -> tuple[int, int, int]:
    """The deterministic color for a kind the palette has never heard of:
    hue = hash(kind) mod 360, at the renderer's light-theme saturation and
    lightness (42%, 68%)."""
    hue = _hash_of(kind) % 360
    red, green, blue = colorsys.hls_to_rgb(hue / 360.0, 0.68, 0.42)
    return (round(red * 255), round(green * 255), round(blue * 255))


def _rgb_of(kind: str, palette: Mapping[str, MapColor]) -> tuple[int, int, int]:
    """The document's own color for ``kind`` if it names one, else engine policy.

    A pair exports its ``light`` value: this file has exactly one theme, and the
    light one is what :data:`PALETTE` already follows.
    """
    authored = palette.get(kind)
    if authored is not None:
        return _hex_rgb(authored.light)
    fixed = PALETTE.get(kind)
    return fixed if fixed is not None else _fallback_rgb(kind)


def _hex_rgb(color: str) -> tuple[int, int, int]:
    """``#rrggbb`` to its channels; the document guarantees the shape."""
    return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))


# --- what each square is ----------------------------------------------------
def _terrain_kinds(
    document: MapDocument,
    plane: MapLevel,
    level: int,
    open_features: Collection[str] | None,
) -> list[list[str]]:
    """The terrain kind of every square, as the fixtures named open leave it.

    The one place this file decides what a square *is*; the walls and the image
    are then two readings of the same grid, so they cannot disagree about a
    square. ``None`` resolves nothing and reads the tiles, as this exporter
    always did. A collection resolves through
    :class:`~fivee_sim.service.maps.ResolvedLevel` — the same derivation
    ``map_render`` and ``map_query`` answer from, rather than a third copy of it.

    **A door's own square is the exception, and it is the format's rule rather
    than ours.** A door travels here as a portal, and a portal buried in solid
    wall is a door the importer cannot open; a shut door's square resolves to
    ``door-closed``, which is opaque, so resolving it would seal the very gap
    the portal exists to fill. What a door *reaches past itself* is spared
    nothing: a sluice gate's flooded room is ordinary fixture business.
    """
    kinds = [[document.legend[char] for char in row] for row in plane.tiles]
    if open_features is None:
        return kinds
    battle = to_grid(document).levels[level]
    live = ResolvedLevel.of(battle, open_features)
    portalled = {
        feature.square for feature in battle.features.values() if feature.kind == "door"
    }
    for y, row in enumerate(kinds):
        for x in range(len(row)):
            if (x, y) not in portalled:
                row[x] = live.terrain_at((x, y))
    return kinds


# --- the PNG ---------------------------------------------------------------
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data))
    )


def _render_png(
    kinds: list[list[str]], palette: Mapping[str, MapColor], pixels_per_grid: int
) -> bytes:
    """A flat-color truecolor PNG of the terrain, one cell per fill.

    Grid lines: when a cell is at least ``_MIN_GRID_PPG`` pixels wide, the
    first pixel row and column of every cell is :data:`GRID_RGB` — a line
    between cells, plus the map's own top and left border.
    """
    width = len(kinds[0])
    grid_lines = pixels_per_grid >= _MIN_GRID_PPG
    grid_pixel = bytes(GRID_RGB)
    grid_row = grid_pixel * (width * pixels_per_grid)

    scanlines: list[bytes] = []
    for row in kinds:
        body = bytearray()
        for kind in row:
            pixel = bytes(_rgb_of(kind, palette))
            if grid_lines:
                body += grid_pixel + pixel * (pixels_per_grid - 1)
            else:
                body += pixel * pixels_per_grid
        body_bytes = bytes(body)
        for py in range(pixels_per_grid):
            scanlines.append(grid_row if grid_lines and py == 0 else body_bytes)

    header = struct.pack(
        ">IIBBBBB", width * pixels_per_grid, len(kinds) * pixels_per_grid, 8, 2, 0, 0, 0
    )
    raw = b"".join(b"\x00" + line for line in scanlines)
    return b"".join(
        [
            _PNG_SIGNATURE,
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(raw, 9)),
            _chunk(b"IEND", b""),
        ]
    )


# --- walls ------------------------------------------------------------------
#: A corner of the cell lattice, in grid-square units.
_Corner = tuple[int, int]

#: The tie-break at a junction: x-direction preferred first.
_DIRECTIONS: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))


def _wall_edges(
    kinds: list[list[str]], terrain: TerrainTable
) -> set[frozenset[_Corner]]:
    """Every interior cell-side where exactly one adjacent cell is opaque,
    as unit edges between integer corners.

    Only interior sides are considered — out of bounds counts as opaque, so
    the map boundary never emits: a wall run along the border contributes
    only its interior-facing edge, and open terrain at the map edge is
    bounded by the map itself rather than by an invented wall.
    """
    height, width = len(kinds), len(kinds[0])
    opaque = [[terrain_effect_of(kind, terrain).opaque for kind in row] for row in kinds]
    edges: set[frozenset[_Corner]] = set()
    for y in range(height):
        for x in range(1, width):
            if opaque[y][x - 1] != opaque[y][x]:
                edges.add(frozenset(((x, y), (x, y + 1))))
    for y in range(1, height):
        for x in range(width):
            if opaque[y - 1][x] != opaque[y][x]:
                edges.add(frozenset(((x, y), (x + 1, y))))
    return edges


def _chain_edges(edges: set[frozenset[_Corner]]) -> list[list[_Corner]]:
    """Unit edges chained into polylines, deterministically.

    Walks start at corners of degree other than two (the endpoints and
    junctions), taken in ``(y, x)`` order; whatever remains is closed loops,
    each started from its ``(y, x)``-smallest corner and closed by repeating
    it. At every step the next edge is chosen x-direction first, and
    collinear consecutive segments merge as the walk goes (a loop's seam is
    not re-merged — the start point stays the start point).
    """
    adjacency: dict[_Corner, set[_Corner]] = {}
    for edge in edges:
        first, second = tuple(edge)
        adjacency.setdefault(first, set()).add(second)
        adjacency.setdefault(second, set()).add(first)

    def step_from(corner: _Corner) -> _Corner:
        for dx, dy in _DIRECTIONS:
            candidate = (corner[0] + dx, corner[1] + dy)
            if candidate in adjacency[corner]:
                return candidate
        raise AssertionError("unit edges always join lattice neighbours")

    def walk(start: _Corner) -> list[_Corner]:
        points = [start]
        heading: tuple[int, int] | None = None
        current = start
        while adjacency[current]:
            following = step_from(current)
            adjacency[current].discard(following)
            adjacency[following].discard(current)
            direction = (following[0] - current[0], following[1] - current[1])
            if direction == heading:
                points[-1] = following
            else:
                points.append(following)
                heading = direction
            current = following
        return points

    polylines: list[list[_Corner]] = []
    by_position = sorted(adjacency, key=lambda corner: (corner[1], corner[0]))
    for seed in (corner for corner in by_position if len(adjacency[corner]) != 2):
        while adjacency[seed]:
            polylines.append(walk(seed))
    for seed in by_position:  # what remains is closed loops
        while adjacency[seed]:
            polylines.append(walk(seed))
    return polylines


# --- the payload ------------------------------------------------------------
def _point(x: float, y: float) -> dict[str, float]:
    return {"x": float(x), "y": float(y)}


def _portals(plane: MapLevel, open_features: Collection[str] | None) -> list[dict[str, Any]]:
    """One portal per door, ``closed`` from ``open_features`` or the record.

    Gated to ``kind == "door"`` on purpose: a portal is a door concept in this
    format, and a lever or a sluice's *spike* is not one. A door that is also a
    fixture — a sluice gate is — is still a door, and travels as this portal
    plus whatever its overlay does to the terrain.
    """
    portals: list[dict[str, Any]] = []
    for feature in sorted(plane.features, key=lambda feature: feature.id):
        if feature.kind != "door":
            continue
        x, y = feature.at
        if feature.orientation == "vertical":
            bounds = [_point(x + 0.5, y), _point(x + 0.5, y + 1.0)]
        else:
            bounds = [_point(x, y + 0.5), _point(x + 1.0, y + 0.5)]
        portals.append(
            {
                "position": _point(x + 0.5, y + 0.5),
                "bounds": bounds,
                "rotation": 0.0,
                "closed": (
                    feature.state == "closed" if open_features is None
                    else feature.id not in open_features
                ),
                "freestanding": False,
            }
        )
    return portals


def to_uvtt(
    document: MapDocument,
    *,
    terrain: TerrainTable,
    pixels_per_grid: int = 32,
    include_image: bool = True,
    level: int = GROUND_LEVEL,
    open: Collection[str] | None = None,
) -> dict[str, Any]:
    """The document as a Universal VTT payload, JSON-ready.

    Every key is always present. ``line_of_sight`` carries the wall polylines
    derived from the terrain; ``portals`` one entry per door feature, ordered by
    feature id; ``image`` a base64 PNG of the map, or ``""`` when
    ``include_image`` is false — documented, because some importers require
    an image and an empty string is a deliberate choice, not an accident.

    ``level`` names the storey to export. The format has one plane and no
    notion of floors, so a map with storeys exports one of them per file rather
    than flattening them into a picture that is true of neither.

    ``open`` names the fixtures standing open — a fight's live set, straight
    from ``fight.map_state.open_features``, and the same argument
    :func:`~fivee_sim.service.maps.render_ascii` takes. Given one, every square
    a fixture claims is exported as that fixture leaves it (see
    :func:`_terrain_kinds`, including the one square it spares) and every portal
    reports the state it is in. Left ``None`` nothing resolves and the export is
    the map exactly as authored; ``None`` and an empty collection are different
    answers, since ``[]`` says every fixture is shut.

    The image refuses to exceed :data:`MAX_IMAGE_SIDE` pixels on a side; the
    error says what ``pixels_per_grid`` would fit. The cap applies whether or
    not the image is rendered, because ``resolution.pixels_per_grid`` declares
    the raster geometry either way.
    """
    if level not in document.levels:
        declared = ", ".join(str(index) for index in sorted(document.levels))
        raise ValueError(f"there is no level {level} on this map. Levels: {declared}")
    plane = document.levels[level]
    width, height = document.grid.width, document.grid.height
    if pixels_per_grid < 1:
        raise ValueError(f"pixels_per_grid must be at least 1, got {pixels_per_grid}")
    if width * pixels_per_grid > MAX_IMAGE_SIDE or height * pixels_per_grid > MAX_IMAGE_SIDE:
        largest = MAX_IMAGE_SIDE // max(width, height)
        if largest < 1:
            raise ValueError(
                f"a {width}x{height}-square map cannot fit a {MAX_IMAGE_SIDE}-pixel "
                f"image even at pixels_per_grid=1"
            )
        raise ValueError(
            f"a {width}x{height}-square map at {pixels_per_grid} pixels per square "
            f"is {width * pixels_per_grid}x{height * pixels_per_grid} pixels, over "
            f"the {MAX_IMAGE_SIDE} cap; lower pixels_per_grid to at most {largest}"
        )

    open_names = None if open is None else frozenset(open)
    if open_names is not None:
        open_names = linked_open_features(to_grid(document).levels[level], open_names)
    kinds = _terrain_kinds(document, plane, level, open_names)
    walls = [
        [_point(corner[0], corner[1]) for corner in polyline]
        for polyline in _chain_edges(_wall_edges(kinds, terrain))
    ]
    image = ""
    if include_image:
        image = base64.b64encode(
            _render_png(kinds, document.palette, pixels_per_grid)
        ).decode("ascii")
    return {
        "format": UVTT_FORMAT,
        "resolution": {
            "map_origin": _point(0.0, 0.0),
            "map_size": _point(width, height),
            "pixels_per_grid": int(pixels_per_grid),
        },
        "line_of_sight": walls,
        "objects_line_of_sight": [],
        "portals": _portals(plane, open_names),
        "environment": {"baked_lighting": False, "ambient_light": "ffffffff"},
        "lights": [],
        "image": image,
    }
