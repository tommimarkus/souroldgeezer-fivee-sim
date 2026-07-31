"""Map documents: validation, canonical serialization, and the battle-map bridge.

Diagnostics are the product, as with content packs: most tests assert on the
message, because being told precisely what is wrong with the JSON is the
feature. The serialization tests pin byte-stability — parse → serialize →
parse yields identical bytes — which is what keeps a saved-but-unchanged map
quiet under version control.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from fivee_sim.kernel.grid import TERRAIN
from fivee_sim.map_document import (
    DEFAULT_LEGEND,
    MAX_MAP_BYTES,
    MAX_MAP_DIM,
    MapColor,
    MapElevation,
    MapError,
    parse_document,
    serialize,
    to_grid,
    validate_document,
)
from fivee_sim.validation import Diagnostic, Severity


def document() -> dict[str, Any]:
    """A small valid document, rebuilt fresh so tests may mutate freely."""
    return {
        "format": "fivee-sim-map",
        "format_version": 1,
        "name": "test-chamber",
        "grid": {"width": 6, "height": 5, "cell_feet": 5},
        "legend": {".": "floor", "#": "wall", "%": "difficult"},
        "tiles": [
            "######",
            "#....#",
            "#.%..#",
            "#....#",
            "###.##",
        ],
        "features": [
            {
                "id": "door-1",
                "kind": "door",
                "at": [3, 4],
                "orientation": "horizontal",
                "state": "closed",
            },
            {"id": "spawn-party", "kind": "spawn", "at": [1, 1], "team": "party"},
        ],
        "provenance": {
            "generator": "hand",
            "seed": 7,
            "params": {"width": 6, "height": 5},
            "edited": False,
            "source": "Authored for the test suite; 5E-compatible original content",
        },
    }


def problems(
    diagnostics: list[Diagnostic], severity: Severity = Severity.ERROR
) -> list[str]:
    return [d.problem for d in diagnostics if d.severity is severity]


def errors_of(payload: dict[str, Any]) -> list[str]:
    return problems(validate_document(payload, source="test", terrain=TERRAIN))


class TestParse:
    def test_a_valid_document_parses_completely(self) -> None:
        doc = parse_document(document(), source="test", terrain=TERRAIN)
        assert doc.name == "test-chamber"
        assert (doc.grid.width, doc.grid.height, doc.grid.cell_feet) == (6, 5, 5)
        assert dict(doc.legend) == {".": "floor", "#": "wall", "%": "difficult"}
        assert doc.tiles == ("######", "#....#", "#.%..#", "#....#", "###.##")
        door, spawn = doc.features
        assert (door.id, door.kind, door.at) == ("door-1", "door", (3, 4))
        assert (door.orientation, door.state, door.team) == ("horizontal", "closed", None)
        assert (spawn.kind, spawn.team) == ("spawn", "party")
        assert doc.provenance.generator == "hand"
        assert doc.provenance.seed == 7
        assert dict(doc.provenance.params) == {"width": 6, "height": 5}
        assert doc.provenance.edited is False

    def test_a_valid_document_yields_no_diagnostics(self) -> None:
        assert validate_document(document(), source="test", terrain=TERRAIN) == []

    def test_parse_raises_with_every_diagnostic(self) -> None:
        payload = document()
        payload["tiles"][1] = "#..?.#"  # unknown glyph
        payload["features"][1]["id"] = "door-1"  # and a duplicate id
        with pytest.raises(MapError) as caught:
            parse_document(payload, source="test", terrain=TERRAIN)
        found = problems(list(caught.value.diagnostics))
        assert len(found) == 2
        assert "2 map error(s)" in str(caught.value)

    def test_default_legend_carries_every_generator_kind(self) -> None:
        for kind in ("floor", "wall", "water", "plain", "forest", "hill", "mountain"):
            assert kind in DEFAULT_LEGEND.values()
        for glyph in DEFAULT_LEGEND:
            assert glyph not in "+/<>@"


class TestDocumentDiagnostics:
    def test_unknown_top_level_key(self) -> None:
        payload = document()
        payload["bogus"] = 1
        assert any("unknown key" in p for p in errors_of(payload))

    def test_wrong_format(self) -> None:
        payload = document()
        payload["format"] = "somebody-elses-map"
        assert any('must be "fivee-sim-map"' in p for p in errors_of(payload))

    def test_wrong_format_version(self) -> None:
        payload = document()
        payload["format_version"] = 2
        assert any("must be 1" in p for p in errors_of(payload))

    def test_missing_name(self) -> None:
        payload = document()
        del payload["name"]
        assert "required" in errors_of(payload)

    def test_blank_name(self) -> None:
        payload = document()
        payload["name"] = "  "
        assert any("non-empty" in p for p in errors_of(payload))

    def test_oversized_document(self) -> None:
        payload = document()
        payload["name"] = "x" * MAX_MAP_BYTES
        assert any(f"{MAX_MAP_BYTES} byte limit" in p for p in errors_of(payload))


class TestGridDiagnostics:
    def test_missing_grid(self) -> None:
        payload = document()
        del payload["grid"]
        assert "required" in errors_of(payload)

    def test_dimension_cap(self) -> None:
        payload = document()
        payload["grid"]["width"] = MAX_MAP_DIM + 1
        assert any(f"at most {MAX_MAP_DIM}" in p for p in errors_of(payload))

    def test_zero_dimension(self) -> None:
        payload = document()
        payload["grid"]["height"] = 0
        assert any("at least 1" in p for p in errors_of(payload))

    def test_cell_feet_is_pinned(self) -> None:
        payload = document()
        payload["grid"]["cell_feet"] = 10
        assert any("must be 5" in p for p in errors_of(payload))

    def test_unknown_grid_key(self) -> None:
        payload = document()
        payload["grid"]["depth"] = 3
        assert any("unknown key" in p for p in errors_of(payload))


class TestLegendDiagnostics:
    def test_reserved_glyph(self) -> None:
        payload = document()
        payload["legend"]["+"] = "door-open"
        found = errors_of(payload)
        assert any("reserved" in p for p in found)
        # The reserved glyph is the whole problem; no cascade about terrain.
        assert len(found) == 1

    def test_unknown_terrain_kind(self) -> None:
        payload = document()
        payload["legend"]["%"] = "lava"
        found = errors_of(payload)
        assert any("'lava'" in p and "does not define" in p for p in found)
        assert any("Available:" in p for p in found)

    def test_multi_character_glyph(self) -> None:
        payload = document()
        payload["legend"]["##"] = "wall"
        assert any("single character" in p for p in errors_of(payload))


class TestTileDiagnostics:
    def test_short_row(self) -> None:
        payload = document()
        payload["tiles"][2] = "#.%.#"
        assert any("row 2 is 5 characters" in p for p in errors_of(payload))

    def test_long_row(self) -> None:
        payload = document()
        payload["tiles"][1] = "#.....#"
        assert any("row 1 is 7 characters" in p for p in errors_of(payload))

    def test_row_count(self) -> None:
        payload = document()
        payload["tiles"].append("######")
        assert any("6 rows" in p and "5 squares high" in p for p in errors_of(payload))

    def test_unknown_glyph_reported_once_with_location(self) -> None:
        payload = document()
        payload["tiles"][1] = "#..?.#"
        payload["tiles"][3] = "#..?.#"
        found = [p for p in errors_of(payload) if "'?'" in p]
        assert found == ["row 1 column 3 uses '?', which the legend does not define"]

    def test_missing_tiles(self) -> None:
        payload = document()
        del payload["tiles"]
        assert "required" in errors_of(payload)


class TestFeatureDiagnostics:
    def test_out_of_bounds(self) -> None:
        payload = document()
        payload["features"][1]["at"] = [6, 2]
        assert any("outside the 6x5 grid" in p for p in errors_of(payload))

    def test_duplicate_id(self) -> None:
        payload = document()
        payload["features"][1]["id"] = "door-1"
        assert any("already used by feature #0" in p for p in errors_of(payload))

    def test_two_doors_on_one_square(self) -> None:
        # The encounter refuses a map whose doors collide, so the document
        # must refuse it first — a valid-looking file may not explode only
        # when a fight starts on it.
        payload = document()
        payload["features"].append(
            {
                "id": "door-2",
                "kind": "door",
                "at": [3, 4],
                "orientation": "vertical",
                "state": "open",
            }
        )
        assert any("already holds door 'door-1'" in p for p in errors_of(payload))

    def test_an_annotation_may_share_a_door_square(self) -> None:
        # Stairs and spawns are annotations, not terrain state; sharing a
        # square is legitimate (a spawn on the stairs, a marker at a door).
        payload = document()
        payload["features"][1]["at"] = [3, 4]  # spawn onto the door square
        assert errors_of(payload) == []

    def test_bad_orientation(self) -> None:
        payload = document()
        payload["features"][0]["orientation"] = "diagonal"
        assert any("horizontal, vertical" in p for p in errors_of(payload))

    def test_bad_state(self) -> None:
        payload = document()
        payload["features"][0]["state"] = "ajar"
        assert any("open, closed" in p for p in errors_of(payload))

    def test_door_requires_orientation_and_state(self) -> None:
        payload = document()
        del payload["features"][0]["orientation"]
        del payload["features"][0]["state"]
        found = errors_of(payload)
        assert any("required for a door" in p for p in found)
        assert len([p for p in found if "door" in p]) == 2

    def test_malformed_at(self) -> None:
        payload = document()
        payload["features"][0]["at"] = [3]
        assert any("must be [x, y]" in p for p in errors_of(payload))

    def test_unknown_feature_key(self) -> None:
        payload = document()
        payload["features"][0]["color"] = "red"
        assert any("unknown key" in p for p in errors_of(payload))


class TestProvenanceDiagnostics:
    def test_missing_provenance(self) -> None:
        payload = document()
        del payload["provenance"]
        assert any("where it came from" in p for p in errors_of(payload))

    def test_missing_fields(self) -> None:
        payload = document()
        payload["provenance"] = {"generator": "hand"}
        found = errors_of(payload)
        # seed, params, edited, and source are each individually reported.
        assert len(found) == 4

    def test_unknown_provenance_key(self) -> None:
        payload = document()
        payload["provenance"]["author"] = "me"
        assert any("unknown key" in p for p in errors_of(payload))


class TestElevation:
    """The optional height layer: absent means flat, and flat writes nothing."""

    def test_a_document_without_the_key_is_flat_at_zero(self) -> None:
        doc = parse_document(document(), source="test", terrain=TERRAIN)
        assert doc.elevation == MapElevation()
        assert doc.elevation.at((3, 2)) == 0

    def test_heights_parse_into_a_sparse_layer(self) -> None:
        payload = document()
        payload["elevation"] = {"default": 0, "squares": [[2, 2, 20], [3, 2, -10]]}
        doc = parse_document(payload, source="test", terrain=TERRAIN)
        assert doc.elevation.default == 0
        assert doc.elevation.at((2, 2)) == 20
        assert doc.elevation.at((3, 2)) == -10  # a pit floor sits below the datum
        assert doc.elevation.at((1, 1)) == 0

    def test_a_flat_map_writes_no_elevation_key(self) -> None:
        # The guarantee that keeps every map saved before heights existed quiet
        # under version control.
        text = serialize(parse_document(document(), source="test", terrain=TERRAIN))
        assert "elevation" not in json.loads(text)

    def test_a_raised_datum_is_written_even_with_no_named_squares(self) -> None:
        payload = document()
        payload["elevation"] = {"default": 30, "squares": []}
        written = json.loads(serialize(parse_document(payload, source="t", terrain=TERRAIN)))
        assert written["elevation"] == {"default": 30, "squares": []}

    def test_squares_at_the_default_are_canonicalised_away(self) -> None:
        payload = document()
        payload["elevation"] = {"default": 5, "squares": [[2, 2, 5], [1, 1, 20]]}
        written = json.loads(serialize(parse_document(payload, source="t", terrain=TERRAIN)))
        assert written["elevation"] == {"default": 5, "squares": [[1, 1, 20]]}

    def test_squares_are_written_in_row_then_column_order(self) -> None:
        payload = document()
        payload["elevation"] = {"squares": [[4, 3, 15], [1, 1, 5], [3, 1, 10]]}
        written = json.loads(serialize(parse_document(payload, source="t", terrain=TERRAIN)))
        assert written["elevation"]["squares"] == [[1, 1, 5], [3, 1, 10], [4, 3, 15]]

    def test_a_document_with_heights_round_trips_byte_stably(self) -> None:
        payload = document()
        payload["elevation"] = {"default": 0, "squares": [[4, 3, 15], [1, 1, 5]]}
        doc = parse_document(payload, source="test", terrain=TERRAIN)
        text = serialize(doc)
        again = parse_document(json.loads(text), source="round-trip", terrain=TERRAIN)
        assert serialize(again) == text

    def test_heights_cross_to_the_battle_map(self) -> None:
        payload = document()
        payload["elevation"] = {"default": 5, "squares": [[2, 2, 20]]}
        grid = to_grid(parse_document(payload, source="test", terrain=TERRAIN))
        assert grid.default_elevation == 5
        assert grid.elevation == {(2, 2): 20}


class TestElevationDiagnostics:
    def test_a_non_object_says_what_the_shape_is(self) -> None:
        payload = document()
        payload["elevation"] = [[1, 1, 5]]
        assert any('"squares"' in p for p in errors_of(payload))

    def test_an_unknown_key_is_refused(self) -> None:
        payload = document()
        payload["elevation"] = {"default": 0, "heights": []}
        assert any("unknown key" in p for p in errors_of(payload))

    def test_a_malformed_entry_names_its_index(self) -> None:
        payload = document()
        payload["elevation"] = {"squares": [[1, 1, 5], [2, 2]]}
        assert any("entry #1 must be [x, y, feet]" in p for p in errors_of(payload))

    def test_a_height_off_the_grid_is_refused(self) -> None:
        payload = document()
        payload["elevation"] = {"squares": [[9, 9, 5]]}
        assert any("outside the 6x5 grid" in p for p in errors_of(payload))

    def test_a_square_named_twice_is_refused(self) -> None:
        payload = document()
        payload["elevation"] = {"squares": [[1, 1, 5], [1, 1, 10]]}
        assert any("names square (1, 1) again" in p for p in errors_of(payload))

    def test_a_non_integer_default_is_refused(self) -> None:
        payload = document()
        payload["elevation"] = {"default": "high"}
        assert any("whole number" in p for p in errors_of(payload))


class TestPalette:
    """The optional color layer: absent means computed, and absent writes nothing."""

    def test_a_document_without_the_key_carries_no_colors(self) -> None:
        doc = parse_document(document(), source="test", terrain=TERRAIN)
        assert dict(doc.palette) == {}

    def test_one_color_serves_both_themes(self) -> None:
        payload = document()
        payload["palette"] = {"water": "#a9c6ce"}
        doc = parse_document(payload, source="test", terrain=TERRAIN)
        assert doc.palette["water"] == MapColor(light="#a9c6ce", dark="#a9c6ce")

    def test_a_pair_colors_each_theme_separately(self) -> None:
        payload = document()
        payload["palette"] = {"water": {"light": "#a9c6ce", "dark": "#1f3a44"}}
        doc = parse_document(payload, source="test", terrain=TERRAIN)
        assert doc.palette["water"] == MapColor(light="#a9c6ce", dark="#1f3a44")

    def test_shorthand_expands_and_case_is_normalised(self) -> None:
        # Canonical storage is what keeps serialize ∘ parse idempotent.
        payload = document()
        payload["palette"] = {"floor": "#ABC"}
        doc = parse_document(payload, source="test", terrain=TERRAIN)
        assert doc.palette["floor"].light == "#aabbcc"

    def test_a_kind_needs_no_glyph_in_this_map(self) -> None:
        # Colors outlive re-legending, so an unpainted kind may still be colored.
        payload = document()
        payload["palette"] = {"water": "#a9c6ce"}
        doc = parse_document(payload, source="test", terrain=TERRAIN)
        assert "water" not in doc.legend.values()
        assert "water" in doc.palette

    def test_a_document_without_colors_writes_no_palette_key(self) -> None:
        # The guarantee that keeps every map saved before colors existed quiet
        # under version control.
        text = serialize(parse_document(document(), source="test", terrain=TERRAIN))
        assert "palette" not in json.loads(text)

    def test_kinds_are_written_in_sorted_order(self) -> None:
        payload = document()
        payload["palette"] = {"water": "#a9c6ce", "floor": "#e9e4d8", "wall": "#4d463c"}
        written = json.loads(serialize(parse_document(payload, source="t", terrain=TERRAIN)))
        assert list(written["palette"]) == ["floor", "wall", "water"]

    def test_a_pair_that_matches_collapses_to_one_color(self) -> None:
        payload = document()
        payload["palette"] = {"floor": {"light": "#AABBCC", "dark": "#aabbcc"}}
        written = json.loads(serialize(parse_document(payload, source="t", terrain=TERRAIN)))
        assert written["palette"] == {"floor": "#aabbcc"}

    def test_a_differing_pair_is_written_light_then_dark(self) -> None:
        payload = document()
        payload["palette"] = {"water": {"dark": "#1f3a44", "light": "#a9c6ce"}}
        written = json.loads(serialize(parse_document(payload, source="t", terrain=TERRAIN)))
        assert list(written["palette"]["water"].items()) == [
            ("light", "#a9c6ce"),
            ("dark", "#1f3a44"),
        ]

    def test_a_colored_document_round_trips_byte_stably(self) -> None:
        payload = document()
        payload["palette"] = {"floor": "#ABC", "water": {"light": "#a9c6ce", "dark": "#1f3a44"}}
        doc = parse_document(payload, source="test", terrain=TERRAIN)
        text = serialize(doc)
        again = parse_document(json.loads(text), source="round-trip", terrain=TERRAIN)
        assert serialize(again) == text


class TestPaletteDiagnostics:
    def test_a_non_object_says_what_the_shape_is(self) -> None:
        payload = document()
        payload["palette"] = ["#a9c6ce"]
        assert any("terrain kinds to colors" in p for p in errors_of(payload))

    def test_an_unknown_terrain_kind_lists_what_is_available(self) -> None:
        payload = document()
        payload["palette"] = {"lava": "#d2440f"}
        assert any(
            "names terrain 'lava', which the active content does not define" in p
            and "Available: " in p
            for p in errors_of(payload)
        )

    def test_a_named_css_color_is_refused(self) -> None:
        payload = document()
        payload["palette"] = {"floor": "red"}
        assert any("must be a hex color" in p for p in errors_of(payload))

    def test_a_url_value_is_refused(self) -> None:
        # The pages assign this into style.background; a url() would fetch over
        # the network and break the editor's offline guarantee.
        payload = document()
        payload["palette"] = {"floor": "url(https://example.invalid/x.png)"}
        assert any("must be a hex color" in p for p in errors_of(payload))

    def test_a_hex_of_the_wrong_length_is_refused(self) -> None:
        payload = document()
        payload["palette"] = {"floor": "#12345"}
        assert any("must be a hex color" in p for p in errors_of(payload))

    def test_a_non_hex_digit_is_refused(self) -> None:
        payload = document()
        payload["palette"] = {"floor": "#gggggg"}
        assert any("must be a hex color" in p for p in errors_of(payload))

    def test_a_pair_missing_a_theme_is_refused(self) -> None:
        payload = document()
        payload["palette"] = {"floor": {"light": "#aabbcc"}}
        assert any('must give both "light" and "dark"' in p for p in errors_of(payload))

    def test_an_unknown_theme_key_is_refused(self) -> None:
        payload = document()
        payload["palette"] = {"floor": {"light": "#aabbcc", "dark": "#112233", "dusk": "#445566"}}
        assert any("unknown key" in p for p in errors_of(payload))

    def test_a_value_of_the_wrong_type_is_refused(self) -> None:
        payload = document()
        payload["palette"] = {"floor": 16711680}
        assert any("must be a hex color" in p for p in errors_of(payload))


class TestSerialize:
    def test_round_trip_is_byte_stable(self) -> None:
        doc = parse_document(document(), source="test", terrain=TERRAIN)
        text = serialize(doc)
        again = parse_document(json.loads(text), source="round-trip", terrain=TERRAIN)
        assert serialize(again) == text

    def test_text_shape(self) -> None:
        text = serialize(parse_document(document(), source="test", terrain=TERRAIN))
        assert text.endswith("}\n")
        assert "\r" not in text

    def test_key_order_is_canonical(self) -> None:
        shuffled = document()
        shuffled["legend"] = dict(reversed(list(shuffled["legend"].items())))
        shuffled["provenance"]["params"] = dict(
            reversed(list(shuffled["provenance"]["params"].items()))
        )
        canonical = serialize(parse_document(document(), source="a", terrain=TERRAIN))
        assert serialize(parse_document(shuffled, source="b", terrain=TERRAIN)) == canonical


class TestToGrid:
    def test_terrain_lands_sparsely_around_the_majority_kind(self) -> None:
        grid = to_grid(parse_document(document(), source="test", terrain=TERRAIN))
        assert (grid.name, grid.width, grid.height) == ("test-chamber", 6, 5)
        assert grid.default_terrain == "wall"  # 17 wall, 12 floor, 1 difficult
        assert (0, 0) not in grid.terrain
        assert grid.terrain[(1, 1)] == "floor"
        assert grid.terrain[(2, 2)] == "difficult"
        assert grid.terrain[(3, 4)] == "floor"  # the doorway is carved floor
        assert len(grid.terrain) == 13
        assert grid.provenance == document()["provenance"]["source"]

    def test_doors_become_features_and_nothing_else_does(self) -> None:
        grid = to_grid(parse_document(document(), source="test", terrain=TERRAIN))
        assert set(grid.features) == {"door-1"}
        door = grid.features["door-1"]
        assert (door.square, door.kind, door.initially_open) == ((3, 4), "door", False)

    def test_an_open_door_starts_open(self) -> None:
        payload = document()
        payload["features"][0]["state"] = "open"
        grid = to_grid(parse_document(payload, source="test", terrain=TERRAIN))
        assert grid.features["door-1"].initially_open is True


def storey() -> dict[str, Any]:
    """One upper floor over the same footprint as :func:`document`."""
    return {
        # Mostly open, where the ground below is mostly wall — so the two planes
        # disagree about their majority terrain and the tests can tell.
        "index": 1,
        "name": "gallery",
        "tiles": [
            "#.....",
            "......",
            "......",
            "......",
            "......",
        ],
        "elevation": {"default": 10, "squares": []},
        "features": [{"id": "stair-head", "kind": "stairs_down", "at": [3, 3],
                      "to_level": 0}],
    }


def with_storey() -> dict[str, Any]:
    """The base document, plus a gallery reached by stairs from the ground."""
    payload = document()
    payload["features"].append(
        {"id": "stair-foot", "kind": "stairs_up", "at": [3, 3], "to_level": 1}
    )
    payload["levels"] = [storey()]
    return payload


class TestLevels:
    def test_a_document_without_levels_is_a_single_ground_plane(self) -> None:
        doc = parse_document(document(), source="test", terrain=TERRAIN)
        assert set(doc.levels) == {0}
        assert doc.ground.index == 0
        assert doc.ground.tiles == doc.tiles

    def test_the_ground_accessors_still_read_the_ground_plane(self) -> None:
        # 50-odd call sites read document.tiles/.features/.elevation and mean
        # the ground; adding storeys must not quietly repoint them.
        doc = parse_document(with_storey(), source="test", terrain=TERRAIN)
        assert doc.tiles == doc.levels[0].tiles
        assert doc.features == doc.levels[0].features
        assert doc.elevation == doc.levels[0].elevation
        assert doc.tiles != doc.levels[1].tiles

    def test_a_storey_parses_over_the_same_footprint(self) -> None:
        doc = parse_document(with_storey(), source="test", terrain=TERRAIN)
        assert set(doc.levels) == {0, 1}
        upper = doc.levels[1]
        assert upper.name == "gallery"
        assert upper.tiles[2] == "......"  # the ground's difficult square is not up here
        assert upper.elevation.at((1, 1)) == 10

    def test_a_storeys_datum_is_its_floor_height(self) -> None:
        doc = parse_document(with_storey(), source="test", terrain=TERRAIN)
        assert doc.levels[0].elevation.at((1, 1)) == 0
        assert doc.levels[1].elevation.at((1, 1)) == 10

    def test_a_storey_may_be_a_basement(self) -> None:
        payload = with_storey()
        payload["levels"][0]["index"] = -1
        payload["levels"][0]["elevation"] = {"default": -10, "squares": []}
        payload["features"][-1]["to_level"] = -1
        payload["levels"][0]["features"][0]["to_level"] = 0
        doc = parse_document(payload, source="test", terrain=TERRAIN)
        assert set(doc.levels) == {-1, 0}
        assert doc.levels[-1].elevation.at((1, 1)) == -10

    def test_the_default_storey_name_is_its_index(self) -> None:
        payload = with_storey()
        del payload["levels"][0]["name"]
        doc = parse_document(payload, source="test", terrain=TERRAIN)
        assert doc.levels[1].name == "level 1"

    def test_two_levels_may_hold_a_door_at_the_same_square(self) -> None:
        payload = with_storey()
        payload["levels"][0]["features"].append(
            {"id": "door-2", "kind": "door", "at": [3, 4],
             "orientation": "horizontal", "state": "closed"}
        )
        assert errors_of(payload) == []

    def test_connectors_survive_the_round_trip(self) -> None:
        doc = parse_document(with_storey(), source="test", terrain=TERRAIN)
        foot = next(f for f in doc.ground.features if f.id == "stair-foot")
        assert (foot.kind, foot.at, foot.to_level) == ("stairs_up", (3, 3), 1)
        head = next(f for f in doc.levels[1].features if f.id == "stair-head")
        assert head.to_level == 0

    def test_an_ordinary_feature_has_no_target_level(self) -> None:
        doc = parse_document(document(), source="test", terrain=TERRAIN)
        assert all(feature.to_level is None for feature in doc.features)


class TestLevelDiagnostics:
    def test_levels_must_be_a_list_of_objects(self) -> None:
        payload = document()
        payload["levels"] = {"index": 1}
        assert any("must be a list" in problem for problem in errors_of(payload))

    def test_index_zero_is_the_ground_and_cannot_be_redeclared(self) -> None:
        payload = with_storey()
        payload["levels"][0]["index"] = 0
        assert any(
            "0 is the ground plane" in problem for problem in errors_of(payload)
        )

    def test_two_levels_cannot_share_an_index(self) -> None:
        payload = with_storey()
        payload["levels"].append(storey())
        assert any("already declared" in problem for problem in errors_of(payload))

    def test_a_level_is_refused_without_an_index(self) -> None:
        payload = with_storey()
        del payload["levels"][0]["index"]
        assert any("height order" in problem for problem in errors_of(payload))

    def test_a_storey_must_match_the_grid(self) -> None:
        payload = with_storey()
        payload["levels"][0]["tiles"] = ["####", "####"]
        problems_found = errors_of(payload)
        assert any("rows" in problem for problem in problems_found)

    def test_a_storey_glyph_must_be_in_the_legend(self) -> None:
        payload = with_storey()
        payload["levels"][0]["tiles"][1] = "#..?.#"
        assert any("legend does not define" in problem for problem in errors_of(payload))

    def test_a_storey_height_must_sit_on_the_grid(self) -> None:
        payload = with_storey()
        payload["levels"][0]["elevation"]["squares"] = [[9, 9, 15]]
        assert any("outside the" in problem for problem in errors_of(payload))

    def test_feature_ids_are_unique_across_every_level(self) -> None:
        # The battle map keys features by name in one table, so two levels
        # sharing an id would resolve to one feature, not two.
        payload = with_storey()
        payload["levels"][0]["features"][0]["id"] = "door-1"
        assert any("ids must be unique" in problem for problem in errors_of(payload))

    def test_a_connector_must_name_a_level_that_exists(self) -> None:
        payload = with_storey()
        payload["features"][-1]["to_level"] = 4
        assert any("no level 4" in problem for problem in errors_of(payload))

    def test_a_connector_cannot_lead_to_its_own_level(self) -> None:
        payload = with_storey()
        payload["features"][-1]["to_level"] = 0
        assert any("its own level" in problem for problem in errors_of(payload))

    def test_a_connector_target_must_be_an_integer(self) -> None:
        payload = with_storey()
        payload["features"][-1]["to_level"] = "up"
        assert any("must name a level" in problem for problem in errors_of(payload))

    def test_an_unknown_level_key_is_refused_with_the_valid_list(self) -> None:
        payload = with_storey()
        payload["levels"][0]["ceiling"] = 12
        assert any(
            "unknown key" in problem and "index, name, tiles" in problem
            for problem in errors_of(payload)
        )


class TestLevelSerialize:
    def test_a_floorless_document_writes_no_levels_key(self) -> None:
        doc = parse_document(document(), source="test", terrain=TERRAIN)
        assert "levels" not in json.loads(serialize(doc))

    def test_the_ground_stays_in_the_top_level_keys(self) -> None:
        written = json.loads(serialize(parse_document(with_storey(), source="t", terrain=TERRAIN)))
        assert written["tiles"] == document()["tiles"]
        assert [level["index"] for level in written["levels"]] == [1]

    def test_storeys_round_trip_byte_stable(self) -> None:
        doc = parse_document(with_storey(), source="test", terrain=TERRAIN)
        text = serialize(doc)
        again = parse_document(json.loads(text), source="round-trip", terrain=TERRAIN)
        assert serialize(again) == text
        assert again == doc

    def test_storeys_are_written_in_index_order(self) -> None:
        payload = with_storey()
        basement = storey()
        basement["index"] = -1
        basement["features"] = []
        payload["levels"].insert(0, basement)
        written = json.loads(serialize(parse_document(payload, source="t", terrain=TERRAIN)))
        assert [level["index"] for level in written["levels"]] == [-1, 1]


class TestToGridLevels:
    def test_a_floorless_map_bridges_to_one_plane(self) -> None:
        grid = to_grid(parse_document(document(), source="test", terrain=TERRAIN))
        assert set(grid.levels) == {0}
        assert grid.ground is grid.levels[0]

    def test_every_storey_becomes_its_own_plane(self) -> None:
        grid = to_grid(parse_document(with_storey(), source="test", terrain=TERRAIN))
        assert set(grid.levels) == {0, 1}
        upper = grid.levels[1]
        assert upper.default_terrain == "floor"  # 29 floor to 1 wall up here
        assert upper.default_elevation == 10
        assert (2, 2) not in upper.terrain  # the ground's difficult square is not up here

    def test_each_plane_picks_its_own_majority_terrain(self) -> None:
        grid = to_grid(parse_document(with_storey(), source="test", terrain=TERRAIN))
        assert grid.ground.default_terrain == "wall"
        assert grid.levels[1].default_terrain == "floor"

    def test_the_ground_accessors_still_read_the_ground_plane(self) -> None:
        grid = to_grid(parse_document(with_storey(), source="test", terrain=TERRAIN))
        assert grid.default_terrain == grid.ground.default_terrain
        assert grid.terrain == grid.ground.terrain
        assert grid.elevation == grid.ground.elevation
        assert grid.default_elevation == grid.ground.default_elevation

    def test_features_merge_across_planes_under_one_name_table(self) -> None:
        payload = with_storey()
        payload["levels"][0]["features"].append(
            {"id": "hatch", "kind": "door", "at": [1, 1],
             "orientation": "horizontal", "state": "closed"}
        )
        grid = to_grid(parse_document(payload, source="test", terrain=TERRAIN))
        assert set(grid.features) == {"door-1", "hatch"}
        assert set(grid.ground.features) == {"door-1"}
        assert set(grid.levels[1].features) == {"hatch"}

    def test_a_connector_reaches_the_plane_it_stands_on(self) -> None:
        grid = to_grid(parse_document(with_storey(), source="test", terrain=TERRAIN))
        assert grid.ground.connectors == {(3, 3): 1}
        assert grid.levels[1].connectors == {(3, 3): 0}

    def test_a_stairway_without_a_target_stays_decoration(self) -> None:
        # Stairs have always been drawn and never walked; only `to_level` makes
        # one a way between planes.
        payload = document()
        payload["features"].append({"id": "stair-1", "kind": "stairs_down", "at": [1, 3]})
        grid = to_grid(parse_document(payload, source="test", terrain=TERRAIN))
        assert grid.ground.connectors == {}
        assert "stair-1" not in grid.features


class TestHandEdited:
    def test_an_edited_document_still_validates_and_keeps_its_provenance(self) -> None:
        payload = document()
        payload["tiles"][2] = "#.%%.#"  # paint one more difficult square
        payload["features"][0]["state"] = "open"  # flip the door's default
        payload["provenance"]["edited"] = True
        assert validate_document(payload, source="edited", terrain=TERRAIN) == []
        doc = parse_document(payload, source="edited", terrain=TERRAIN)
        assert doc.provenance.edited is True
        assert doc.provenance.seed == 7  # generation lineage rides through the edit
        assert dict(doc.provenance.params) == {"width": 6, "height": 5}
        grid = to_grid(doc)
        assert grid.terrain[(3, 2)] == "difficult"
        assert grid.features["door-1"].initially_open is True
