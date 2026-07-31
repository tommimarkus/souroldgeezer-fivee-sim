"""Universal VTT export: wall derivation, portals, the PNG, and determinism.

The wall test that matters is the property one: every emitted polyline,
rasterized back to unit edges, must exactly cover the set of interior
cell-sides where an opaque square meets a non-opaque one — computed here by
an independent oracle over the raw tiles, so the chaining and merging can
never hide a dropped or invented wall.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import zlib
from typing import Any

import pytest

from fivee_sim.kernel.grid import TERRAIN
from fivee_sim.map_document import MapDocument, parse_document
from fivee_sim.service.uvtt import (
    GRID_RGB,
    MAX_IMAGE_SIDE,
    PALETTE,
    UVTT_FORMAT,
    _fallback_rgb,
    _hash_of,
    to_uvtt,
)

Corner = tuple[int, int]
Edge = frozenset[Corner]


def payload() -> dict[str, Any]:
    """The 6x6 fixture: a wall run from the map edge broken by a doorway,
    a larger wall mass, and a fully enclosed one-square room (a closed loop)."""
    return {
        "format": "fivee-sim-map",
        "format_version": 1,
        "name": "uvtt fixture",
        "grid": {"width": 6, "height": 6, "cell_feet": 5},
        "legend": {".": "floor", "#": "wall"},
        "tiles": [
            "..#...",
            "..#...",
            "......",
            "..####",
            "...#.#",
            "...###",
        ],
        "features": [
            {
                "id": "door-1",
                "kind": "door",
                "at": [2, 2],
                "orientation": "vertical",
                "state": "closed",
            },
        ],
        "provenance": {
            "generator": "hand",
            "seed": 0,
            "params": {},
            "edited": False,
            "source": "Authored for the test suite; 5E-compatible original content",
        },
    }


def document(raw: dict[str, Any] | None = None) -> MapDocument:
    return parse_document(raw or payload(), source="fixture", terrain=TERRAIN)


def export(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("pixels_per_grid", 8)
    return to_uvtt(document(), terrain=TERRAIN, **kwargs)


def rasterize(walls: list[list[dict[str, float]]]) -> set[Edge]:
    """Every polyline back to unit edges, asserting integer axis-aligned
    segments and that no unit edge is emitted twice."""
    edges: set[Edge] = set()
    for line in walls:
        points = [(point["x"], point["y"]) for point in line]
        assert len(points) >= 2
        for raw_a, raw_b in zip(points, points[1:], strict=False):
            assert all(value == int(value) for value in (*raw_a, *raw_b))
            ax, ay = int(raw_a[0]), int(raw_a[1])
            bx, by = int(raw_b[0]), int(raw_b[1])
            assert ax == bx or ay == by, "polyline segments are axis-aligned"
            if ax == bx:
                step = 1 if by > ay else -1
                units = [frozenset({(ax, y), (ax, y + step)}) for y in range(ay, by, step)]
            else:
                step = 1 if bx > ax else -1
                units = [frozenset({(x, ay), (x + step, ay)}) for x in range(ax, bx, step)]
            for unit in units:
                assert unit not in edges, "no unit edge is emitted twice"
                edges.add(unit)
    return edges


def interior_boundary_edges(tiles: list[str]) -> set[Edge]:
    """The oracle: interior cell-sides whose two cells differ in opacity."""
    opaque = [[char == "#" for char in row] for row in tiles]
    height, width = len(tiles), len(tiles[0])
    edges: set[Edge] = set()
    for y in range(height):
        for x in range(1, width):
            if opaque[y][x - 1] != opaque[y][x]:
                edges.add(frozenset({(x, y), (x, y + 1)}))
    for y in range(1, height):
        for x in range(width):
            if opaque[y - 1][x] != opaque[y][x]:
                edges.add(frozenset({(x, y), (x + 1, y)}))
    return edges


class TestShape:
    def test_every_key_is_present_with_its_type(self) -> None:
        result = export()
        assert result["format"] == UVTT_FORMAT
        assert isinstance(result["format"], float)
        resolution = result["resolution"]
        assert resolution["map_origin"] == {"x": 0.0, "y": 0.0}
        assert resolution["map_size"] == {"x": 6.0, "y": 6.0}
        assert resolution["pixels_per_grid"] == 8
        assert isinstance(result["line_of_sight"], list)
        for line in result["line_of_sight"]:
            for point in line:
                assert isinstance(point["x"], float) and isinstance(point["y"], float)
        assert result["objects_line_of_sight"] == []
        assert isinstance(result["portals"], list)
        assert result["environment"] == {"baked_lighting": False, "ambient_light": "ffffffff"}
        assert result["lights"] == []
        assert isinstance(result["image"], str) and result["image"]

    def test_the_payload_is_json_serializable(self) -> None:
        json.dumps(export())


class TestWalls:
    def test_walls_exactly_cover_the_interior_opacity_boundaries(self) -> None:
        emitted = rasterize(export(include_image=False)["line_of_sight"])
        assert emitted == interior_boundary_edges(payload()["tiles"])

    def test_a_straight_wall_merges_to_a_single_two_point_polyline(self) -> None:
        raw = payload()
        raw["grid"] = {"width": 5, "height": 2, "cell_feet": 5}
        raw["tiles"] = ["#####", "....."]
        raw["features"] = []
        result = to_uvtt(document(raw), terrain=TERRAIN, include_image=False)
        # The wall row's outer sides sit on the map boundary and emit nothing;
        # its five interior-facing unit edges merge into one segment.
        assert result["line_of_sight"] == [
            [{"x": 0.0, "y": 1.0}, {"x": 5.0, "y": 1.0}]
        ]

    def test_an_enclosed_room_is_a_closed_loop(self) -> None:
        walls = export(include_image=False)["line_of_sight"]
        loops = [line for line in walls if line[0] == line[-1]]
        assert loops == [
            [
                {"x": 4.0, "y": 4.0},
                {"x": 5.0, "y": 4.0},
                {"x": 5.0, "y": 5.0},
                {"x": 4.0, "y": 5.0},
                {"x": 4.0, "y": 4.0},
            ]
        ]

    def test_a_map_with_no_opaque_terrain_emits_no_walls(self) -> None:
        raw = payload()
        raw["tiles"] = ["." * 6] * 6
        raw["features"] = []
        result = to_uvtt(document(raw), terrain=TERRAIN, include_image=False)
        assert result["line_of_sight"] == []


class TestPortals:
    def test_the_doorway_breaks_the_wall_and_becomes_one_portal(self) -> None:
        result = export(include_image=False)
        edges = rasterize(result["line_of_sight"])
        # The door square (2, 2) is floor: the edges that would seal the
        # corridor across it must not exist.
        assert frozenset({(2, 2), (2, 3)}) not in edges
        assert frozenset({(3, 2), (3, 3)}) not in edges
        assert result["portals"] == [
            {
                "position": {"x": 2.5, "y": 2.5},
                "bounds": [{"x": 2.5, "y": 2.0}, {"x": 2.5, "y": 3.0}],
                "rotation": 0.0,
                "closed": True,
                "freestanding": False,
            }
        ]

    def test_a_horizontal_open_door_spans_its_square_the_other_way(self) -> None:
        raw = payload()
        raw["features"] = [
            {"id": "door-h", "kind": "door", "at": [4, 2],
             "orientation": "horizontal", "state": "open"},
        ]
        (portal,) = to_uvtt(document(raw), terrain=TERRAIN, include_image=False)["portals"]
        assert portal["bounds"] == [{"x": 4.0, "y": 2.5}, {"x": 5.0, "y": 2.5}]
        assert portal["closed"] is False

    def test_portals_are_ordered_by_feature_id(self) -> None:
        raw = payload()
        raw["features"] = [
            {"id": "z-door", "kind": "door", "at": [2, 2],
             "orientation": "vertical", "state": "closed"},
            {"id": "a-door", "kind": "door", "at": [4, 2],
             "orientation": "horizontal", "state": "open"},
            {"id": "stair", "kind": "stairs_down", "at": [0, 0]},
        ]
        portals = to_uvtt(document(raw), terrain=TERRAIN, include_image=False)["portals"]
        assert [portal["position"]["x"] for portal in portals] == [4.5, 2.5]


class TestImage:
    def decode(self, image: str) -> tuple[int, int, bytes]:
        png = base64.b64decode(image)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", png[16:24])
        assert png[24] == 8 and png[25] == 2  # 8-bit truecolor
        offset, idat = 8, b""
        while offset < len(png):
            (length,) = struct.unpack(">I", png[offset:offset + 4])
            kind = png[offset + 4:offset + 8]
            if kind == b"IDAT":
                idat += png[offset + 8:offset + 8 + length]
            offset += 12 + length
        return width, height, zlib.decompress(idat)

    def pixel(self, raw: bytes, width: int, px: int, py: int) -> tuple[int, int, int]:
        stride = 1 + width * 3
        row = raw[py * stride:(py + 1) * stride]
        assert row[0] == 0  # filter type: none
        return (row[1 + px * 3], row[2 + px * 3], row[3 + px * 3])

    def test_dimensions_follow_pixels_per_grid(self) -> None:
        width, height, raw = self.decode(export(pixels_per_grid=8)["image"])
        assert (width, height) == (48, 48)
        assert len(raw) == 48 * (1 + 48 * 3)

    def test_terrain_fills_and_the_grid_line(self) -> None:
        width, _, raw = self.decode(export(pixels_per_grid=8)["image"])
        assert self.pixel(raw, width, 4, 4) == PALETTE["floor"]  # cell (0, 0)
        assert self.pixel(raw, width, 2 * 8 + 4, 4) == PALETTE["wall"]  # cell (2, 0)
        assert self.pixel(raw, width, 8, 4) == GRID_RGB  # first column of cell (1, 0)

    def test_a_pack_kind_gets_the_renderers_hash_hue(self) -> None:
        # h = h * 31 + code over a uint32, hue = h mod 360 — renderer.js's
        # documented fallback formula, reimplemented rather than ported.
        assert _hash_of("swamp") == 109846752
        assert _hash_of("swamp") % 360 == 312
        assert _fallback_rgb("swamp") == _fallback_rgb("swamp")
        assert _fallback_rgb("swamp") != _fallback_rgb("bog")

    def test_include_image_false_is_an_empty_string(self) -> None:
        assert export(include_image=False)["image"] == ""

    def test_an_oversized_image_is_refused_with_the_remedy(self) -> None:
        with pytest.raises(ValueError, match="lower pixels_per_grid to at most 682"):
            export(pixels_per_grid=683)
        with pytest.raises(ValueError, match="at least 1"):
            export(pixels_per_grid=0)
        assert MAX_IMAGE_SIDE == 4096


class TestDeterminism:
    def test_the_same_document_dumps_byte_identically(self) -> None:
        first = json.dumps(export(), sort_keys=True)
        second = json.dumps(export(), sort_keys=True)
        assert first == second

    def test_the_golden_payload(self) -> None:
        # A declared-break canary: this pins the exported payload of the 6x6
        # fixture byte for byte (image omitted for readability; the full
        # payload including the PNG is pinned by hash below). If either
        # assertion fails, the export format changed — that may be right, but
        # it must be a decision, not an accident.
        bare = json.dumps(export(include_image=False), sort_keys=True)
        assert bare == (
            '{"environment": {"ambient_light": "ffffffff", "baked_lighting": false}, '
            '"format": 0.3, "image": "", "lights": [], '
            '"line_of_sight": ['
            '[{"x": 2.0, "y": 0.0}, {"x": 2.0, "y": 2.0}, '
            '{"x": 3.0, "y": 2.0}, {"x": 3.0, "y": 0.0}], '
            '[{"x": 6.0, "y": 3.0}, {"x": 2.0, "y": 3.0}, {"x": 2.0, "y": 4.0}, '
            '{"x": 3.0, "y": 4.0}, {"x": 3.0, "y": 6.0}], '
            '[{"x": 4.0, "y": 4.0}, {"x": 5.0, "y": 4.0}, {"x": 5.0, "y": 5.0}, '
            '{"x": 4.0, "y": 5.0}, {"x": 4.0, "y": 4.0}]], '
            '"objects_line_of_sight": [], '
            '"portals": [{"bounds": [{"x": 2.5, "y": 2.0}, {"x": 2.5, "y": 3.0}], '
            '"closed": true, "freestanding": false, '
            '"position": {"x": 2.5, "y": 2.5}, "rotation": 0.0}], '
            '"resolution": {"map_origin": {"x": 0.0, "y": 0.0}, '
            '"map_size": {"x": 6.0, "y": 6.0}, "pixels_per_grid": 8}}'
        )
        full = json.dumps(export(), sort_keys=True)
        assert (
            hashlib.sha256(full.encode("utf-8")).hexdigest()
            == "3c4f400df8a21462603f3b8e74c0ed2cb6e58f065af01a7dfb27cc1683c34eed"
        )
