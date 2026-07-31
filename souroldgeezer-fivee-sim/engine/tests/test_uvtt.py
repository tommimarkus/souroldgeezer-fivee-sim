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


def decode(image: str) -> tuple[int, int, bytes]:
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


def pixel(raw: bytes, width: int, px: int, py: int) -> tuple[int, int, int]:
    stride = 1 + width * 3
    row = raw[py * stride:(py + 1) * stride]
    assert row[0] == 0  # filter type: none
    return (row[1 + px * 3], row[2 + px * 3], row[3 + px * 3])


def square(raw: bytes, width: int, cell: tuple[int, int], ppg: int = 8) -> tuple[int, int, int]:
    """The fill of one grid square, sampled clear of its grid line."""
    return pixel(raw, width, cell[0] * ppg + ppg // 2, cell[1] * ppg + ppg // 2)


def gated_payload() -> dict[str, Any]:
    """A 4x3 room whose only wall is a portcullis, and a puddle beside it.

    Every tile is floor. The gate is a wall while it is shut and floor once it
    is raised, and the square east of it is dry floor until the same gate lets
    the water in — and nothing but the gate's own record says either, which is
    what makes an export that reads the tiles alone unable to show it.
    """
    raw = payload()
    raw["name"] = "portcullis room"
    raw["grid"] = {"width": 4, "height": 3, "cell_feet": 5}
    raw["tiles"] = ["....", "....", "...."]
    raw["features"] = [
        {
            "id": "portcullis", "kind": "portcullis", "at": [1, 1], "state": "closed",
            "terrain": {"closed": "wall", "open": "floor"},
            "affects": [
                {"cells": [[2, 1]], "terrain": {"closed": "floor", "open": "water"}},
            ],
        }
    ]
    return raw


#: The four unit edges around the single opaque square at (1, 1), all interior.
GATE_SHUT_EDGES = {
    frozenset({(1, 1), (1, 2)}),
    frozenset({(2, 1), (2, 2)}),
    frozenset({(1, 1), (2, 1)}),
    frozenset({(1, 2), (2, 2)}),
}


class TestFixtureStates:
    """``open``: export the map a fight is on, not only the map as authored.

    The same argument ``render_ascii`` takes, and the same two-way distinction:
    ``None`` resolves nothing and reads the tiles, ``[]`` says every fixture is
    shut — which is a different answer for a map that authored one open.
    """

    def gated(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("include_image", False)
        return to_uvtt(document(gated_payload()), terrain=TERRAIN, **kwargs)

    def test_a_shut_fixture_puts_its_wall_on_the_map(self) -> None:
        assert rasterize(self.gated(open=[])["line_of_sight"]) == GATE_SHUT_EDGES

    def test_an_open_fixture_takes_its_wall_off(self) -> None:
        assert self.gated(open=["portcullis"])["line_of_sight"] == []

    def test_open_none_reads_the_tiles_and_resolves_nothing(self) -> None:
        # The gate is authored shut, and this answer is still no wall: None is
        # "do not ask", not "ask about the authored state".
        assert self.gated()["line_of_sight"] == []

    def test_a_name_this_map_has_no_fixture_for_is_ignored(self) -> None:
        # A fight's open set spans every storey, and this export is one level.
        assert self.gated(open=["portcullis", "the cellar hatch"])["line_of_sight"] == []

    def test_a_fixture_moves_the_png_fill_with_it(self) -> None:
        doc = document(gated_payload())
        width, _, shut = decode(
            to_uvtt(doc, terrain=TERRAIN, pixels_per_grid=8, open=[])["image"]
        )
        _, _, raised = decode(
            to_uvtt(doc, terrain=TERRAIN, pixels_per_grid=8, open=["portcullis"])["image"]
        )
        # The gate's own square, and then the square its overlay governs.
        assert square(shut, width, (1, 1)) == PALETTE["wall"]
        assert square(raised, width, (1, 1)) == PALETTE["floor"]
        assert square(shut, width, (2, 1)) == PALETTE["floor"]
        assert square(raised, width, (2, 1)) == PALETTE["water"]

    def test_a_portal_takes_its_state_from_the_argument(self) -> None:
        doc = document()  # door-1, authored closed
        def closed(**kwargs: Any) -> bool:
            portal = to_uvtt(doc, terrain=TERRAIN, include_image=False, **kwargs)["portals"][0]
            return bool(portal["closed"])

        assert closed() is True  # the recorded default
        assert closed(open=["door-1"]) is False
        assert closed(open=[]) is True

    def test_linked_door_portals_always_export_one_shared_state(self) -> None:
        raw = payload()
        raw["features"] = [
            {
                "id": "left",
                "kind": "door",
                "at": [2, 2],
                "orientation": "horizontal",
                "hinge": "west",
                "swing": "north",
                "state": "closed",
                "linked_to": "right",
            },
            {
                "id": "right",
                "kind": "door",
                "at": [3, 2],
                "orientation": "horizontal",
                "hinge": "east",
                "swing": "north",
                "state": "closed",
                "linked_to": "left",
            },
        ]
        doc = document(raw)

        for named in (["left"], ["right"]):
            portals = to_uvtt(doc, terrain=TERRAIN, include_image=False, open=named)["portals"]
            assert [portal["closed"] for portal in portals] == [False, False]

        shut = to_uvtt(doc, terrain=TERRAIN, include_image=False, open=[])["portals"]
        assert [portal["closed"] for portal in shut] == [True, True]

    def test_a_shut_door_stays_a_gap_in_the_wall_rather_than_sealing_it(self) -> None:
        # The one square resolution deliberately does not touch. A door travels
        # as a portal here, and a portal buried in solid wall is a door the
        # importer cannot open — but a shut door's own square resolves to
        # 'door-closed', which is opaque. So the tile under a door is what the
        # walls and the picture both read, exactly as before ``open`` existed.
        edges = rasterize(export(include_image=False, open=[])["line_of_sight"])
        assert edges == interior_boundary_edges(payload()["tiles"])
        assert frozenset({(2, 2), (2, 3)}) not in edges
        width, _, image = decode(export(pixels_per_grid=8, open=[])["image"])
        assert square(image, width, (2, 2)) == PALETTE["floor"]

    def test_a_doors_overlay_resolves_like_any_other_fixtures(self) -> None:
        # Only the door's *own* square is spared: what it reaches past itself is
        # ordinary fixture business, which is what a sluice gate is.
        raw = payload()
        raw["features"][0]["affects"] = [
            {"cells": [[4, 0]], "terrain": {"closed": "floor", "open": "wall"}},
        ]
        doc = document(raw)
        shut = rasterize(
            to_uvtt(doc, terrain=TERRAIN, include_image=False, open=[])["line_of_sight"]
        )
        opened = rasterize(
            to_uvtt(doc, terrain=TERRAIN, include_image=False, open=["door-1"])[
                "line_of_sight"
            ]
        )
        assert shut == interior_boundary_edges(payload()["tiles"])
        assert opened - shut == {
            frozenset({(4, 0), (4, 1)}),
            frozenset({(5, 0), (5, 1)}),
            frozenset({(4, 1), (5, 1)}),
        }
        assert shut - opened == set()

    def test_the_named_storey_is_the_one_resolved(self) -> None:
        # The plane a fixture stands on is the plane its claims land on, so a
        # level-1 export must not be resolved against the ground's fixtures.
        raw = gated_payload()
        raw["levels"] = [
            {
                "index": 1,
                "name": "gallery",
                "tiles": ["....", "....", "...."],
                "features": [
                    {"id": "gallery gate", "kind": "portcullis", "at": [2, 1],
                     "state": "closed", "terrain": {"closed": "wall", "open": "floor"}},
                ],
            }
        ]
        doc = document(raw)
        upper = to_uvtt(
            doc, terrain=TERRAIN, include_image=False, level=1, open=["portcullis"]
        )
        # The ground's gate is open and irrelevant; the gallery's is shut.
        assert rasterize(upper["line_of_sight"]) == {
            frozenset({(2, 1), (2, 2)}),
            frozenset({(3, 1), (3, 2)}),
            frozenset({(2, 1), (3, 1)}),
            frozenset({(2, 2), (3, 2)}),
        }


class TestImage:
    def test_dimensions_follow_pixels_per_grid(self) -> None:
        width, height, raw = decode(export(pixels_per_grid=8)["image"])
        assert (width, height) == (48, 48)
        assert len(raw) == 48 * (1 + 48 * 3)

    def test_terrain_fills_and_the_grid_line(self) -> None:
        width, _, raw = decode(export(pixels_per_grid=8)["image"])
        assert pixel(raw, width, 4, 4) == PALETTE["floor"]  # cell (0, 0)
        assert pixel(raw, width, 2 * 8 + 4, 4) == PALETTE["wall"]  # cell (2, 0)
        assert pixel(raw, width, 8, 4) == GRID_RGB  # first column of cell (1, 0)

    def test_a_pack_kind_gets_the_renderers_hash_hue(self) -> None:
        # h = h * 31 + code over a uint32, hue = h mod 360 — renderer.js's
        # documented fallback formula, reimplemented rather than ported.
        assert _hash_of("swamp") == 109846752
        assert _hash_of("swamp") % 360 == 312
        assert _fallback_rgb("swamp") == _fallback_rgb("swamp")
        assert _fallback_rgb("swamp") != _fallback_rgb("bog")

    def test_the_documents_palette_outranks_the_engine_table(self) -> None:
        raw = payload()
        raw["palette"] = {"floor": "#d2440f"}
        width, _, image = decode(
            to_uvtt(document(raw), terrain=TERRAIN, pixels_per_grid=8)["image"]
        )
        assert pixel(image, width, 4, 4) == (0xD2, 0x44, 0x0F)
        assert pixel(image, width, 2 * 8 + 4, 4) == PALETTE["wall"]  # uncolored

    def test_a_theme_pair_exports_its_light_color(self) -> None:
        # The PNG has exactly one theme, and the light one is what the engine
        # table already follows.
        raw = payload()
        raw["palette"] = {"floor": {"light": "#a9c6ce", "dark": "#1f3a44"}}
        width, _, image = decode(
            to_uvtt(document(raw), terrain=TERRAIN, pixels_per_grid=8)["image"]
        )
        assert pixel(image, width, 4, 4) == (0xA9, 0xC6, 0xCE)

    def test_a_pack_kind_can_be_colored_instead_of_hashed(self) -> None:
        raw = payload()
        raw["legend"]["~"] = "water"
        raw["tiles"][0] = "~.#..."
        raw["palette"] = {"water": "#010203"}
        width, _, image = decode(
            to_uvtt(document(raw), terrain=TERRAIN, pixels_per_grid=8)["image"]
        )
        assert pixel(image, width, 4, 4) == (0x01, 0x02, 0x03)

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
