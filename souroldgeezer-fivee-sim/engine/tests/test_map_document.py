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

from fivee_sim.kernel.grid import TERRAIN, Facing
from fivee_sim.kernel.rules import Ability
from fivee_sim.map_document import (
    DEFAULT_LEGEND,
    MAX_MAP_BYTES,
    MAX_MAP_DIM,
    RESERVED_GLYPHS,
    MapColor,
    MapDocument,
    MapElevation,
    MapError,
    MapFeatureRecord,
    MapOverlayRecord,
    allocate_legend,
    as_payload,
    parse_document,
    serialize,
    to_grid,
    validate_document,
)
from fivee_sim.model.battlemap import (
    BattleMap,
    FeatureCheck,
    FeatureOverlay,
    FeatureTrigger,
    HeightPair,
    TerrainPair,
    TriggerMode,
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


def double_doors() -> dict[str, Any]:
    """A valid reciprocal horizontal pair, rebuilt for mutation by each test."""
    payload = document()
    payload["features"][0] = {
        "id": "door-left",
        "kind": "door",
        "at": [3, 4],
        "orientation": "horizontal",
        "hinge": "west",
        "swing": "north",
        "state": "closed",
        "linked_to": "door-right",
    }
    payload["features"].insert(
        1,
        {
            "id": "door-right",
            "kind": "door",
            "at": [4, 4],
            "orientation": "horizontal",
            "hinge": "east",
            "swing": "north",
            "state": "closed",
            "linked_to": "door-left",
        },
    )
    return payload


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
        with pytest.raises(MapError, match="2 map error") as caught:
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

    def test_door_swing_metadata_is_orientation_specific(self) -> None:
        payload = document()
        payload["features"][0].update({"hinge": "east", "swing": "south"})
        door = parse_document(payload, source="test", terrain=TERRAIN).features[0]
        assert (door.hinge, door.swing, door.linked_to) == ("east", "south", None)

        payload["features"][0].update(
            {"orientation": "vertical", "hinge": "south", "swing": "east"}
        )
        assert errors_of(payload) == []

        payload["features"][0]["hinge"] = "west"
        assert any("vertical door hinge" in p for p in errors_of(payload))
        payload["features"][0].update({"hinge": "south", "swing": "north"})
        assert any("vertical door swing" in p for p in errors_of(payload))

    def test_only_doors_may_carry_swing_or_link_metadata(self) -> None:
        payload = document()
        payload["features"][1].update(
            {"hinge": "west", "swing": "north", "linked_to": "door-1"}
        )
        found = errors_of(payload)
        assert any("only a door may carry 'hinge'" in p for p in found)
        assert any("only a door may carry 'swing'" in p for p in found)
        assert any("only a door may carry 'linked_to'" in p for p in found)

    def test_a_reciprocal_aligned_pair_parses(self) -> None:
        doc = parse_document(double_doors(), source="test", terrain=TERRAIN)
        left, right = doc.features[:2]
        assert left.linked_to == "door-right"
        assert right.linked_to == "door-left"

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ((1, "linked_to", "door-missing"), "has no feature with that id"),
            ((1, "linked_to", None), "must link back"),
            ((1, "orientation", "vertical"), "same orientation"),
            ((1, "at", [3, 3]), "adjacent along their shared orientation"),
            ((1, "state", "open"), "same state"),
            ((1, "costs_action", True), "same interaction contract"),
        ],
    )
    def test_linked_door_invariants(
        self, change: tuple[int, str, Any], message: str
    ) -> None:
        payload = double_doors()
        index, key, value = change
        if value is None:
            del payload["features"][index][key]
        else:
            payload["features"][index][key] = value
        if key == "orientation":
            payload["features"][index].update({"hinge": "south", "swing": "east"})
        assert any(message in p for p in errors_of(payload))

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


class TestCompass:
    """Where *true* north lies. Absent means grid north, and absent writes nothing."""

    def test_a_document_without_the_key_faces_grid_north(self) -> None:
        doc = parse_document(document(), source="test", terrain=TERRAIN)
        assert doc.compass is Facing.NORTH

    def test_a_compass_parses_onto_the_document(self) -> None:
        payload = document()
        payload["compass"] = "east"
        doc = parse_document(payload, source="test", terrain=TERRAIN)
        assert doc.compass is Facing.EAST

    @pytest.mark.parametrize("name", [facing.value for facing in Facing])
    def test_every_one_of_the_eight_names_is_accepted(self, name: str) -> None:
        payload = document()
        payload["compass"] = name
        assert parse_document(payload, source="test", terrain=TERRAIN).compass == name

    def test_a_document_facing_grid_north_writes_no_compass_key(self) -> None:
        # The guarantee that keeps every map saved before the compass existed
        # quiet under version control.
        text = serialize(parse_document(document(), source="test", terrain=TERRAIN))
        assert "compass" not in json.loads(text)

    def test_declaring_the_default_writes_no_key_either(self) -> None:
        # Omission is a rule about the *value*, not about the key's absence: a
        # file that spells out grid north writes back the bytes a file that
        # never mentioned it does.
        payload = document()
        payload["compass"] = "north"
        with_key = serialize(parse_document(payload, source="a", terrain=TERRAIN))
        without = serialize(parse_document(document(), source="a", terrain=TERRAIN))
        assert with_key == without

    def test_a_declared_compass_is_written_beside_the_grid(self) -> None:
        payload = document()
        payload["compass"] = "southwest"
        written = json.loads(serialize(parse_document(payload, source="t", terrain=TERRAIN)))
        assert written["compass"] == "southwest"
        assert list(written)[:5] == ["format", "format_version", "name", "grid", "compass"]

    def test_a_document_with_a_compass_round_trips_byte_stably(self) -> None:
        payload = document()
        payload["compass"] = "northwest"
        doc = parse_document(payload, source="test", terrain=TERRAIN)
        text = serialize(doc)
        again = parse_document(json.loads(text), source="round-trip", terrain=TERRAIN)
        assert serialize(again) == text
        assert again == doc


class TestCompassDoesNotRedefineGridNorth:
    """The decision the whole vocabulary rests on, pinned rather than assumed.

    Four of the eight names are already spent on disk, on door hinge and swing,
    and they mean −y and +y. A compass that could re-aim them would silently
    change the meaning of every map already saved, so it does not: it says where
    true north lies for a rose and for narration, and door validation never
    consults it.
    """

    def horizontal_door(self, compass: str, **keys: str) -> dict[str, Any]:
        payload = document()
        payload["compass"] = compass
        payload["features"][0].update(keys)
        return payload

    def test_a_document_facing_east_still_refuses_a_horizontal_door_hinged_north(
        self,
    ) -> None:
        payload = self.horizontal_door("east", hinge="north")
        assert any(
            "a horizontal door hinge must be one of: west, east" in p
            for p in errors_of(payload)
        )

    def test_a_document_facing_east_still_refuses_a_horizontal_door_swinging_east(
        self,
    ) -> None:
        # The mirror case: 'east' is the compass's own answer, and the door
        # vocabulary is no more willing to take it than before.
        payload = self.horizontal_door("east", swing="east")
        assert any(
            "a horizontal door swing must be one of: north, south" in p
            for p in errors_of(payload)
        )

    def test_the_grid_relative_hinge_and_swing_stay_valid_under_any_compass(self) -> None:
        for name in (facing.value for facing in Facing):
            payload = self.horizontal_door(name, hinge="west", swing="north")
            doc = parse_document(payload, source="test", terrain=TERRAIN)
            assert (doc.features[0].hinge, doc.features[0].swing) == ("west", "north")
            assert doc.compass == name


class TestCompassDiagnostics:
    def test_an_unknown_name_lists_the_eight(self) -> None:
        payload = document()
        payload["compass"] = "up"
        assert any(
            "'up' is not valid; must be one of: north, northeast, east, southeast, "
            "south, southwest, west, northwest" in p
            for p in errors_of(payload)
        )

    def test_a_non_string_compass_is_refused(self) -> None:
        payload = document()
        payload["compass"] = 90
        assert any("must be one of: north" in p for p in errors_of(payload))

    def test_a_bad_compass_stops_the_document_parsing(self) -> None:
        payload = document()
        payload["compass"] = "nor'east"
        with pytest.raises(MapError, match="must be one of: north"):
            parse_document(payload, source="test", terrain=TERRAIN)

    def test_a_level_may_not_carry_one(self) -> None:
        # A storey is a floor of one building, not a map of its own: two
        # storeys disagreeing about where true north lies is not a thing a
        # building can do.
        payload = document()
        payload["levels"] = [
            {"index": 1, "name": "upper", "tiles": payload["tiles"], "compass": "east"}
        ]
        # Matched on the level's own key list, not on "unknown key" alone: the
        # document refusing the key would say the same first two words.
        assert any(
            "Valid keys: ambient_light, elevation, features, index, name, tiles" in p
            for p in errors_of(payload)
        )


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


def sluice() -> dict[str, Any]:
    """A gate that floods the chamber, and the spike that holds it shut.

    The spike stands on a wall square and names no terrain of its own, which is
    the case that pins a fixture taking its own tile's kind in both states — a
    lever on a wall stays a wall.
    """
    payload = document()
    payload["features"] = [
        {
            "id": "spike",
            "kind": "spike",
            "at": [0, 4],
            "state": "closed",
            "costs_action": True,
            "check": {"ability": "strength", "dc": 15},
        },
        {
            "id": "gate",
            "kind": "door",
            "at": [3, 4],
            "orientation": "horizontal",
            "state": "closed",
            "requires": ["spike"],
            "costs_action": True,
            "terrain": {"closed": "door-closed", "open": "water"},
            "elevation": {"closed": 0, "open": -5},
            "affects": [
                {
                    "cells": [[2, 3], [1, 3]],
                    "terrain": {"closed": "floor", "open": "water"},
                    "elevation": {"closed": 0, "open": -5},
                },
                {"cells": [[4, 1]], "terrain": {"closed": "floor", "open": "difficult"}},
            ],
        },
        {"id": "spawn-party", "kind": "spawn", "at": [1, 1], "team": "party"},
    ]
    return payload


def triggered_sluice(*, mode: str = "maintained") -> dict[str, Any]:
    """The sluice opens when its spike is pulled."""
    payload = sluice()
    payload["features"][1]["trigger"] = {
        "when": {"spike": "open"},
        "set": "open",
        "mode": mode,
    }
    return payload


class TestFeatureFixtures:
    """The seven optional keys that make a feature something a fight can operate."""

    def test_a_document_with_no_fixture_keys_writes_the_bytes_it_always_did(self) -> None:
        # The additive-optional-key promise: format_version stays 1 because every
        # file written before fixtures existed writes back byte-for-byte.
        written = json.loads(serialize(parse_document(document(), source="t", terrain=TERRAIN)))
        assert written["features"] == document()["features"]
        assert list(written["features"][0]) == ["id", "kind", "at", "orientation", "state"]
        assert list(written["features"][1]) == ["id", "kind", "at", "team"]

    def test_the_six_keys_parse_onto_the_record(self) -> None:
        doc = parse_document(sluice(), source="test", terrain=TERRAIN)
        gate = next(f for f in doc.features if f.id == "gate")
        assert gate.terrain == TerrainPair(closed="door-closed", open="water")
        assert gate.elevation == HeightPair(closed=0, open=-5)
        assert gate.requires == ("spike",)
        assert gate.costs_action is True
        spike = next(f for f in doc.features if f.id == "spike")
        assert spike.check == FeatureCheck(ability=Ability.STRENGTH, dc=15)
        assert gate.affects == (
            MapOverlayRecord(
                cells=((1, 3), (2, 3)),
                terrain=TerrainPair(closed="floor", open="water"),
                elevation=HeightPair(closed=0, open=-5),
            ),
            MapOverlayRecord(
                cells=((4, 1),),
                terrain=TerrainPair(closed="floor", open="difficult"),
            ),
        )

    def test_a_feature_that_carries_none_of_them_carries_none_of_them(self) -> None:
        doc = parse_document(document(), source="test", terrain=TERRAIN)
        spawn = next(f for f in doc.features if f.id == "spawn-party")
        assert (spawn.terrain, spawn.elevation, spawn.check) == (None, None, None)
        assert (spawn.affects, spawn.requires, spawn.costs_action) == ((), (), False)

    def test_a_fixture_document_round_trips_byte_stably(self) -> None:
        doc = parse_document(sluice(), source="test", terrain=TERRAIN)
        text = serialize(doc)
        again = parse_document(json.loads(text), source="round-trip", terrain=TERRAIN)
        assert serialize(again) == text
        assert again == doc

    def test_cells_are_written_in_row_then_column_order(self) -> None:
        payload = sluice()
        payload["features"][1]["affects"][0]["cells"] = [[2, 3], [3, 2], [1, 3]]
        written = json.loads(serialize(parse_document(payload, source="t", terrain=TERRAIN)))
        gate = next(f for f in written["features"] if f["id"] == "gate")
        assert gate["affects"][0]["cells"] == [[3, 2], [1, 3], [2, 3]]

    def test_the_keys_are_written_in_the_documents_own_order(self) -> None:
        written = json.loads(serialize(parse_document(sluice(), source="t", terrain=TERRAIN)))
        gate = next(f for f in written["features"] if f["id"] == "gate")
        assert list(gate) == [
            "id", "kind", "at", "orientation", "state",
            "terrain", "elevation", "affects", "requires", "costs_action",
        ]
        assert list(gate["affects"][0]) == ["cells", "terrain", "elevation"]

    def test_door_swing_and_link_keys_have_a_canonical_order(self) -> None:
        written = json.loads(
            serialize(parse_document(double_doors(), source="test", terrain=TERRAIN))
        )
        assert list(written["features"][0]) == [
            "id", "kind", "at", "orientation", "hinge", "swing", "state", "linked_to",
        ]

    def test_an_overlay_may_move_only_the_height(self) -> None:
        # Either pair alone is a whole overlay: a floodgate draining a cistern
        # lowers the water without changing what the squares are.
        payload = sluice()
        payload["features"][1]["affects"][1] = {
            "cells": [[4, 1]],
            "elevation": {"closed": 0, "open": -5},
        }
        doc = parse_document(payload, source="test", terrain=TERRAIN)
        gate = next(f for f in doc.features if f.id == "gate")
        assert gate.affects[1] == MapOverlayRecord(
            cells=((4, 1),), elevation=HeightPair(closed=0, open=-5)
        )
        written = json.loads(serialize(doc))
        entry = next(f for f in written["features"] if f["id"] == "gate")["affects"][1]
        assert list(entry) == ["cells", "elevation"]

    def test_a_fixture_that_costs_nothing_writes_no_costs_action_key(self) -> None:
        payload = sluice()
        payload["features"][1]["costs_action"] = False
        written = json.loads(serialize(parse_document(payload, source="t", terrain=TERRAIN)))
        gate = next(f for f in written["features"] if f["id"] == "gate")
        assert "costs_action" not in gate


class TestFeatureFacing:
    """Which way a feature points, in the same eight names everything else uses."""

    def facing_statue(self, facing: str = "east") -> dict[str, Any]:
        payload = document()
        payload["features"].append(
            {"id": "statue", "kind": "statue", "at": [4, 3], "facing": facing}
        )
        return payload

    def test_a_facing_parses_onto_the_record(self) -> None:
        doc = parse_document(self.facing_statue(), source="test", terrain=TERRAIN)
        assert doc.features[-1].facing == "east"

    @pytest.mark.parametrize("name", [facing.value for facing in Facing])
    def test_every_one_of_the_eight_names_is_accepted(self, name: str) -> None:
        doc = parse_document(self.facing_statue(name), source="test", terrain=TERRAIN)
        assert doc.features[-1].facing == name

    def test_a_feature_that_does_not_point_carries_none(self) -> None:
        doc = parse_document(document(), source="test", terrain=TERRAIN)
        assert [f.facing for f in doc.features] == [None, None]

    def test_facing_is_written_between_at_and_orientation(self) -> None:
        payload = self.facing_statue()
        payload["features"][-1].update({"orientation": "vertical", "team": "foe"})
        written = json.loads(serialize(parse_document(payload, source="t", terrain=TERRAIN)))
        assert list(written["features"][-1]) == [
            "id", "kind", "at", "facing", "orientation", "team",
        ]

    def test_a_feature_without_one_writes_no_facing_key(self) -> None:
        written = json.loads(serialize(parse_document(document(), source="t", terrain=TERRAIN)))
        assert all("facing" not in entry for entry in written["features"])

    def test_a_facing_document_round_trips_byte_stably(self) -> None:
        doc = parse_document(self.facing_statue("southwest"), source="test", terrain=TERRAIN)
        text = serialize(doc)
        again = parse_document(json.loads(text), source="round-trip", terrain=TERRAIN)
        assert serialize(again) == text
        assert again == doc

    def test_facing_claims_no_square(self) -> None:
        # A feature points at a square; it does not govern it. Two fixtures may
        # face each other across one, and neither has taken it — which is the
        # rule that would have to break for facing to enter _check_claims.
        payload = document()
        payload["features"].extend(
            [
                {"id": "west spike", "kind": "spike", "at": [2, 3],
                 "facing": "east", "state": "closed"},
                {"id": "east spike", "kind": "spike", "at": [4, 3],
                 "facing": "west", "state": "closed"},
            ]
        )
        doc = parse_document(payload, source="test", terrain=TERRAIN)
        assert [f.facing for f in doc.features[-2:]] == ["east", "west"]


class TestFeatureFacingDiagnostics:
    def test_an_unknown_name_lists_the_eight(self) -> None:
        payload = document()
        payload["features"][1]["facing"] = "widdershins"
        assert any(
            "'widdershins' is not valid; must be one of: north, northeast, east, "
            "southeast, south, southwest, west, northwest" in p
            for p in errors_of(payload)
        )

    def test_a_non_string_facing_is_refused(self) -> None:
        payload = document()
        payload["features"][1]["facing"] = 90
        assert any("must be one of: north" in p for p in errors_of(payload))

    def test_a_door_may_not_carry_one(self) -> None:
        # A door already answers this three ways over, and the format refuses a
        # second answer to one question the way it refuses a fixture with no
        # state: silently preferring one of them is the failure to avoid.
        payload = document()
        payload["features"][0]["facing"] = "north"
        assert any(
            "only a feature that is not a door may carry 'facing'" in p
            for p in errors_of(payload)
        )

    def test_the_door_refusal_names_what_already_points(self) -> None:
        payload = document()
        payload["features"][0]["facing"] = "north"
        assert any(
            "orientation, hinge and swing already say where a door points" in p
            for p in errors_of(payload)
        )

    def test_a_bad_facing_drops_the_record_rather_than_half_reading_it(self) -> None:
        payload = document()
        payload["features"][1]["facing"] = "up"
        with pytest.raises(MapError, match="must be one of: north"):
            parse_document(payload, source="test", terrain=TERRAIN)


class TestFeatureTriggers:
    def test_a_trigger_parses_and_round_trips_canonically(self) -> None:
        payload = triggered_sluice()
        payload["features"].append(
            {"id": "alarm", "kind": "lever", "at": [4, 4], "state": "closed"}
        )
        payload["features"][1]["trigger"]["when"] = {
            "spike": "open",
            "alarm": "closed",
        }

        document = parse_document(payload, source="test", terrain=TERRAIN)
        gate = next(feature for feature in document.features if feature.id == "gate")

        assert gate.trigger is not None
        assert gate.trigger.when == (("alarm", False), ("spike", True))
        assert gate.trigger.set_open is True
        assert gate.trigger.mode.value == "maintained"
        written = json.loads(serialize(document))
        written_gate = next(feature for feature in written["features"] if feature["id"] == "gate")
        assert written_gate["trigger"] == {
            "when": {"alarm": "closed", "spike": "open"},
            "set": "open",
            "mode": "maintained",
        }
        assert list(written_gate).index("trigger") == list(written_gate).index("requires") + 1
        assert serialize(
            parse_document(written, source="round-trip", terrain=TERRAIN)
        ) == serialize(document)

    @pytest.mark.parametrize(
        ("trigger", "message"),
        [
            (None, "trigger must be an object"),
            ([], "trigger must be an object"),
            ({"set": "open", "mode": "edge"}, "when is required"),
            ({"when": [], "set": "open", "mode": "edge"}, "when must be an object"),
            ({"when": {}, "set": "open", "mode": "edge"}, "at least one fixture"),
            (
                {"when": {"spike": "ajar"}, "set": "open", "mode": "edge"},
                "must be one of: open, closed",
            ),
            ({"when": {"spike": "open"}, "mode": "edge"}, "set is required"),
            (
                {"when": {"spike": "open"}, "set": "ajar", "mode": "edge"},
                "set must be one of: open, closed",
            ),
            (
                {"when": {"spike": "open"}, "set": "open", "mode": "pulse"},
                "mode must be one of: edge, maintained",
            ),
        ],
    )
    def test_malformed_triggers_are_refused(
        self, trigger: Any, message: str
    ) -> None:
        payload = sluice()
        payload["features"][1]["trigger"] = trigger
        assert any(message in problem for problem in errors_of(payload))

    def test_a_trigger_reference_must_exist(self) -> None:
        missing = triggered_sluice()
        missing["features"][1]["trigger"]["when"] = {"ghost": "open"}
        assert any(
            "trigger references 'ghost', but there is no feature 'ghost'" in problem
            for problem in errors_of(missing)
        )

    def test_a_trigger_reference_must_carry_state(self) -> None:
        stateless = triggered_sluice()
        stateless["features"][1]["trigger"]["when"] = {"spawn-party": "open"}
        assert any(
            "trigger references 'spawn-party', which carries no state" in problem
            for problem in errors_of(stateless)
        )

    def test_trigger_dependencies_must_be_acyclic(self) -> None:
        payload = triggered_sluice(mode="edge")
        payload["features"][0]["trigger"] = {
            "when": {"gate": "open"},
            "set": "open",
            "mode": "edge",
        }
        found = [problem for problem in errors_of(payload) if "trigger cycle" in problem]
        assert found == [
            "feature 'gate' is in a trigger cycle: gate -> spike -> gate; "
            "automatic fixture transitions must be acyclic"
        ]

    def test_linked_door_leaves_must_have_identical_triggers(self) -> None:
        payload = double_doors()
        payload["features"].append(
            {"id": "lever", "kind": "lever", "at": [1, 1], "state": "closed"}
        )
        payload["features"][0]["trigger"] = {
            "when": {"lever": "open"},
            "set": "open",
            "mode": "maintained",
        }
        assert any(
            "linked doors must have identical triggers" in problem
            for problem in errors_of(payload)
        )

    def test_an_opening_trigger_must_imply_every_requirement(self) -> None:
        payload = triggered_sluice()
        payload["features"].append(
            {"id": "second spike", "kind": "spike", "at": [4, 3], "state": "closed"}
        )
        payload["features"][1]["requires"] = ["spike", "second spike"]
        assert any(
            "opens feature 'gate' but does not require 'second spike' to be open" in problem
            for problem in errors_of(payload)
        )

    def test_a_true_maintained_trigger_must_match_the_authored_state(self) -> None:
        payload = triggered_sluice()
        payload["features"][1]["trigger"]["when"] = {"spike": "closed"}
        assert any(
            "maintained trigger is true initially and sets it open, but its state is closed"
            in problem
            for problem in errors_of(payload)
        )

    def test_a_true_edge_trigger_does_not_constrain_the_authored_state(self) -> None:
        payload = triggered_sluice(mode="edge")
        payload["features"][1]["requires"] = []
        payload["features"][1]["trigger"]["when"] = {"spike": "closed"}
        assert errors_of(payload) == []

    def test_a_trigger_without_a_state_is_refused_as_a_fixture_key(self) -> None:
        payload = triggered_sluice()
        del payload["features"][1]["state"]
        assert any(
            "required for a feature carrying" in problem and "trigger" in problem
            for problem in errors_of(payload)
        )


class TestFixturesCrossToTheBattleMap:
    """``state``, not ``kind``, is what makes a feature one the fight owns."""

    def test_a_non_door_carrying_a_state_becomes_a_feature(self) -> None:
        grid = to_grid(parse_document(sluice(), source="test", terrain=TERRAIN))
        assert set(grid.features) == {"spike", "gate"}
        assert grid.features["spike"].kind == "spike"

    def test_a_non_door_carrying_no_state_stays_document_level(self) -> None:
        # The other side of the same rule: a spawn hint is an annotation, and a
        # drawn stairway goes on being drawn rather than operated.
        payload = sluice()
        payload["features"].append({"id": "stair-1", "kind": "stairs_down", "at": [4, 3]})
        grid = to_grid(parse_document(payload, source="test", terrain=TERRAIN))
        assert "stair-1" not in grid.features
        assert "spawn-party" not in grid.features

    def test_a_fixture_without_terrain_takes_its_own_tile_in_both_states(self) -> None:
        # The regression guard for a hand-written file that already carries a
        # state on a non-door: a spike driven into a wall stays a wall.
        spike = to_grid(parse_document(sluice(), source="test", terrain=TERRAIN)).features["spike"]
        assert (spike.closed_terrain, spike.open_terrain) == ("wall", "wall")

    def test_an_upper_storeys_fixture_takes_the_upper_storeys_tile(self) -> None:
        """The same rule, one floor up, where it is possible to get it wrong.

        The case above stands on the ground plane, where "this level's tile" and
        "the document's tiles" are the same string — so it passes against a
        bridge that reads ``document.tiles`` whatever storey it was asked about,
        and every lever, spike and pressure plate upstairs would silently take
        the terrain of the room below it.

        The gallery is open floor at (2, 2); the chamber beneath it is
        ``difficult`` there. One square, two answers, and only one of them is
        this fixture's.
        """
        payload = with_storey()
        payload["levels"][0]["features"].append(
            {"id": "gallery-lever", "kind": "lever", "at": [2, 2], "state": "closed"}
        )
        doc = parse_document(payload, source="test", terrain=TERRAIN)
        assert doc.levels[0].tiles[2][2] == "%"  # difficult, downstairs
        assert doc.levels[1].tiles[2][2] == "."  # floor, in the gallery

        lever = to_grid(doc).features["gallery-lever"]
        assert (lever.closed_terrain, lever.open_terrain) == ("floor", "floor")

    def test_a_door_without_terrain_keeps_the_door_pair(self) -> None:
        grid = to_grid(parse_document(document(), source="test", terrain=TERRAIN))
        door = grid.features["door-1"]
        assert (door.closed_terrain, door.open_terrain) == ("door-closed", "door-open")

    def test_link_and_orientation_cross_to_the_runtime_map(self) -> None:
        battle = to_grid(parse_document(double_doors(), source="test", terrain=TERRAIN))
        assert battle.features["door-left"].linked_to == "door-right"
        assert battle.features["door-right"].linked_to == "door-left"
        assert battle.features["door-left"].orientation == "horizontal"
        assert battle.features["door-right"].orientation == "horizontal"

    def test_an_authored_pair_overrides_the_default(self) -> None:
        gate = to_grid(parse_document(sluice(), source="test", terrain=TERRAIN)).features["gate"]
        assert (gate.closed_terrain, gate.open_terrain) == ("door-closed", "water")
        assert gate.elevation == HeightPair(closed=0, open=-5)

    def test_overlays_cross_as_runtime_overlays(self) -> None:
        gate = to_grid(parse_document(sluice(), source="test", terrain=TERRAIN)).features["gate"]
        assert gate.affects == (
            FeatureOverlay(
                squares=((1, 3), (2, 3)),
                terrain=TerrainPair(closed="floor", open="water"),
                elevation=HeightPair(closed=0, open=-5),
            ),
            FeatureOverlay(
                squares=((4, 1),),
                terrain=TerrainPair(closed="floor", open="difficult"),
            ),
        )

    def test_what_operating_it_costs_and_takes_crosses_too(self) -> None:
        grid = to_grid(parse_document(sluice(), source="test", terrain=TERRAIN))
        assert grid.features["gate"].requires == ("spike",)
        assert grid.features["gate"].costs_action is True
        assert grid.features["spike"].check == FeatureCheck(ability=Ability.STRENGTH, dc=15)

    def test_a_fixture_on_a_storey_lands_on_its_own_plane(self) -> None:
        payload = with_storey()
        payload["levels"][0]["features"].append(
            {"id": "lever", "kind": "lever", "at": [1, 1], "state": "closed"}
        )
        grid = to_grid(parse_document(payload, source="test", terrain=TERRAIN))
        assert set(grid.levels[1].features) == {"lever"}
        assert "lever" not in grid.ground.features


class TestFixturePairDiagnostics:
    def test_a_terrain_pair_must_be_an_object(self) -> None:
        payload = sluice()
        payload["features"][1]["terrain"] = "water"
        assert any("naming the terrain kind in each state" in p for p in errors_of(payload))

    def test_a_terrain_pair_needs_both_states(self) -> None:
        payload = sluice()
        del payload["features"][1]["terrain"]["closed"]
        assert any(
            'must give both "closed" and "open" terrain kinds' in p for p in errors_of(payload)
        )

    def test_a_terrain_pair_refuses_a_kind_the_content_does_not_define(self) -> None:
        payload = sluice()
        payload["features"][1]["terrain"]["open"] = "lava"
        found = errors_of(payload)
        assert any(
            "names terrain 'lava', which the active content does not define" in p for p in found
        )
        assert any("Available:" in p for p in found)

    def test_a_terrain_kind_is_checked_against_content_not_this_legend(self) -> None:
        # `water` has no glyph in this document, and that is fine: a fixture may
        # turn its square into any kind the loaded content defines, exactly as a
        # palette may color one this map never paints.
        payload = sluice()
        assert "water" not in payload["legend"].values()
        assert errors_of(payload) == []

    def test_a_terrain_pair_must_name_kinds_as_text(self) -> None:
        payload = sluice()
        payload["features"][1]["terrain"]["open"] = 7
        assert any("must name a terrain kind, got 7" in p for p in errors_of(payload))

    def test_an_unknown_key_in_a_pair_is_refused(self) -> None:
        payload = sluice()
        payload["features"][1]["terrain"]["ajar"] = "water"
        assert any(
            "unknown key" in p and "Valid keys: closed, open" in p for p in errors_of(payload)
        )

    def test_an_elevation_pair_must_be_an_object(self) -> None:
        payload = sluice()
        payload["features"][1]["elevation"] = [0, -5]
        assert any("ground height in feet in each state" in p for p in errors_of(payload))

    def test_an_elevation_pair_needs_both_states(self) -> None:
        payload = sluice()
        del payload["features"][1]["elevation"]["open"]
        assert any(
            'must give both "closed" and "open" heights in feet' in p for p in errors_of(payload)
        )

    def test_an_elevation_pair_must_be_whole_feet(self) -> None:
        payload = sluice()
        payload["features"][1]["elevation"]["open"] = "deep"
        assert any("must be a whole number, got 'deep'" in p for p in errors_of(payload))


class TestOverlayDiagnostics:
    def test_affects_must_be_a_list(self) -> None:
        payload = sluice()
        payload["features"][1]["affects"] = {"cells": [[1, 3]]}
        assert any("must be a list of overlay objects" in p for p in errors_of(payload))

    def test_an_overlay_must_be_an_object(self) -> None:
        payload = sluice()
        payload["features"][1]["affects"] = [[1, 3]]
        assert any("entry #0 must be an object" in p for p in errors_of(payload))

    def test_an_overlay_needs_cells(self) -> None:
        payload = sluice()
        del payload["features"][1]["affects"][0]["cells"]
        assert any("the squares this overlay governs" in p for p in errors_of(payload))

    def test_an_overlay_needs_at_least_one_cell(self) -> None:
        payload = sluice()
        payload["features"][1]["affects"][0]["cells"] = []
        assert any("must name at least one square" in p for p in errors_of(payload))

    def test_cells_must_be_a_list(self) -> None:
        payload = sluice()
        payload["features"][1]["affects"][0]["cells"] = "1,3"
        assert any("must be a list of [x, y] squares" in p for p in errors_of(payload))

    def test_a_malformed_cell_names_its_index(self) -> None:
        payload = sluice()
        payload["features"][1]["affects"][0]["cells"] = [[1, 3], [2]]
        assert any("cell #1 must be [x, y] square indices" in p for p in errors_of(payload))

    def test_a_cell_off_the_grid_is_refused(self) -> None:
        payload = sluice()
        payload["features"][1]["affects"][0]["cells"] = [[9, 9]]
        assert any("cell #0 is at (9, 9), outside the 6x5 grid" in p for p in errors_of(payload))

    def test_an_overlay_that_moves_neither_layer_is_refused(self) -> None:
        payload = sluice()
        payload["features"][1]["affects"][1] = {"cells": [[4, 1]]}
        assert any("needs 'terrain', 'elevation', or both" in p for p in errors_of(payload))

    def test_an_overlay_takes_cells_and_never_a_rect(self) -> None:
        # A rect is an edit-op convenience; the document stores the squares it
        # expanded to, so one file has one shape and a resize can translate it.
        payload = sluice()
        payload["features"][1]["affects"][0]["rect"] = [1, 3, 2, 1]
        assert any(
            "unknown key" in p and "Valid keys: cells, elevation, terrain" in p
            for p in errors_of(payload)
        )


class TestClaimDiagnostics:
    """Every square a fixture governs is governed by exactly one, per level."""

    def test_two_fixtures_cannot_claim_one_square(self) -> None:
        payload = sluice()
        payload["features"][0]["at"] = [1, 3]  # the spike, standing in the flood
        assert any(
            "feature 'gate' claims square (1, 3), which feature 'spike' already governs; "
            "one fixture per square" in p
            for p in errors_of(payload)
        )

    def test_two_overlays_cannot_claim_one_square(self) -> None:
        payload = sluice()
        payload["features"][0]["affects"] = [
            {"cells": [[2, 3]], "terrain": {"closed": "floor", "open": "water"}}
        ]
        assert any(
            "feature 'gate' claims square (2, 3), which feature 'spike' already governs" in p
            for p in errors_of(payload)
        )

    def test_a_fixture_cannot_claim_a_square_twice(self) -> None:
        payload = sluice()
        payload["features"][1]["affects"][1]["cells"] = [[3, 4]]  # the gate's own square
        assert any(
            "feature 'gate' claims square (3, 4) twice; a fixture decides each square once" in p
            for p in errors_of(payload)
        )

    def test_two_levels_may_hold_a_fixture_on_the_same_square(self) -> None:
        payload = with_storey()
        payload["levels"][0]["features"].append(
            {"id": "lever", "kind": "lever", "at": [3, 4], "state": "closed"}
        )
        assert errors_of(payload) == []

    def test_an_annotation_may_stand_in_a_governed_square(self) -> None:
        # Spawns and drawn stairs claim nothing: they carry no state, so no
        # fixture is contradicted by one sitting in a room that floods.
        payload = sluice()
        payload["features"][2]["at"] = [1, 3]
        assert errors_of(payload) == []


class TestRequiresDiagnostics:
    def test_requires_must_be_a_list_of_names(self) -> None:
        payload = sluice()
        payload["features"][1]["requires"] = "spike"
        assert any("must be a list of names" in p for p in errors_of(payload))

    def test_a_requirement_must_name_a_feature_the_map_has(self) -> None:
        payload = sluice()
        payload["features"][1]["requires"] = ["south spike"]
        found = errors_of(payload)
        assert any(
            "feature 'gate' requires 'south spike', but there is no feature "
            "'south spike' in this map" in p
            for p in found
        )
        assert any("Declared: gate, spawn-party, spike" in p for p in found)

    def test_a_fixture_cannot_require_itself(self) -> None:
        payload = sluice()
        payload["features"][1]["requires"] = ["gate"]
        assert any(
            "feature 'gate' requires itself; a prerequisite is another fixture" in p
            for p in errors_of(payload)
        )

    def test_a_requirement_must_name_something_that_can_stand_open(self) -> None:
        payload = sluice()
        payload["features"][1]["requires"] = ["spawn-party"]
        assert any(
            "requires 'spawn-party', which carries no state and so is never open" in p
            for p in errors_of(payload)
        )

    def test_a_requirement_cycle_is_reported_once_from_its_smallest_id(self) -> None:
        # Attached to the lexicographically smallest id in the cycle, so which
        # of the two the author edited last does not change the report.
        payload = sluice()
        payload["features"][0]["requires"] = ["gate"]  # the spike now waits on the gate
        found = [p for p in errors_of(payload) if "requirement cycle" in p]
        assert found == [
            "feature 'gate' is in a requirement cycle: gate -> spike -> gate; "
            "nothing in it could ever be opened first"
        ]

    def test_a_longer_cycle_is_reported_with_its_whole_path(self) -> None:
        payload = sluice()
        payload["features"].append(
            {"id": "chain", "kind": "chain", "at": [4, 3], "state": "closed",
             "requires": ["gate"]}
        )
        payload["features"][0]["requires"] = ["chain"]  # gate -> spike -> chain -> gate
        assert any(
            "requirement cycle: chain -> gate -> spike -> chain" in p for p in errors_of(payload)
        )

    def test_a_requirement_may_name_a_fixture_on_another_storey(self) -> None:
        payload = with_storey()
        payload["features"][0]["requires"] = ["hatch"]
        payload["levels"][0]["features"].append(
            {"id": "hatch", "kind": "door", "at": [1, 1],
             "orientation": "horizontal", "state": "closed"}
        )
        assert errors_of(payload) == []


class TestCheckDiagnostics:
    def test_a_check_must_be_an_object(self) -> None:
        payload = sluice()
        payload["features"][0]["check"] = "strength 15"
        assert any("naming an ability and a DC" in p for p in errors_of(payload))

    def test_a_check_needs_an_ability(self) -> None:
        payload = sluice()
        del payload["features"][0]["check"]["ability"]
        assert any("the ability the check rolls" in p for p in errors_of(payload))

    def test_an_ability_outside_the_six_is_refused(self) -> None:
        payload = sluice()
        payload["features"][0]["check"]["ability"] = "luck"
        assert any(
            "'luck' is not valid; must be one of: strength" in p for p in errors_of(payload)
        )

    def test_a_check_needs_a_dc(self) -> None:
        payload = sluice()
        del payload["features"][0]["check"]["dc"]
        assert any(
            "the difficulty class the check is made against" in p for p in errors_of(payload)
        )

    def test_a_dc_below_one_is_refused(self) -> None:
        payload = sluice()
        payload["features"][0]["check"]["dc"] = 0
        assert any("must be at least 1, got 0" in p for p in errors_of(payload))

    def test_an_unknown_key_in_a_check_is_refused(self) -> None:
        payload = sluice()
        payload["features"][0]["check"]["advantage"] = True
        assert any(
            "unknown key" in p and "Valid keys: ability, dc" in p for p in errors_of(payload)
        )


class TestFixtureStateDiagnostics:
    def test_costs_action_must_be_true_or_false(self) -> None:
        payload = sluice()
        payload["features"][1]["costs_action"] = "yes"
        assert any("must be true or false, got 'yes'" in p for p in errors_of(payload))

    def test_a_fixture_key_without_a_state_is_refused(self) -> None:
        # Carrying a state is what makes a feature one the fight owns, so a
        # feature that says what operating it costs but cannot be operated is a
        # silent no-op — refused rather than dropped on the way to the grid.
        payload = sluice()
        del payload["features"][0]["state"]
        found = errors_of(payload)
        assert any(
            "required for a feature carrying check, costs_action; only a feature with "
            "a state is one a fight can operate" in p
            for p in found
        )

    def test_every_fixture_problem_comes_back_from_one_call(self) -> None:
        # The house rule: a file with four mistakes reports four, not the first.
        payload = sluice()
        payload["features"][0]["check"]["dc"] = 0
        payload["features"][0]["costs_action"] = "yes"
        payload["features"][1]["terrain"]["open"] = "lava"
        payload["features"][1]["affects"][0]["cells"] = [[9, 9]]
        assert len(errors_of(payload)) == 4


class TestAllocateLegend:
    """Glyphs for terrain kinds, chosen so the document that carries them parses.

    The allocator is the document format's own, not a caller's convenience: a
    legend that claimed a renderer's overlay mark would be refused by the parser
    that has to read it back, so the one rule it may never break is
    :data:`RESERVED_GLYPHS`.
    """

    def test_every_kind_gets_a_distinct_single_character(self) -> None:
        legend = allocate_legend(["wall", "floor", "water", "difficult"])

        assert sorted(legend.values()) == ["difficult", "floor", "wall", "water"]
        assert all(len(glyph) == 1 for glyph in legend)

    def test_no_allocated_glyph_is_one_the_renderers_reserve(self) -> None:
        # The whole reason this is the format's function rather than a caller's.
        legend = allocate_legend([f"kind-{index}" for index in range(120)])

        assert not RESERVED_GLYPHS & set(legend)
        assert len(legend) == 120

    def test_an_authors_glyph_is_kept_where_it_stands(self) -> None:
        # A legend somebody wrote is not rewritten for tidiness: only a glyph
        # the format refuses is moved.
        legend = allocate_legend(
            ["wall", "water"], prefer={"W": "wall", "~": "water"}
        )

        assert legend == {"W": "wall", "~": "water"}

    def test_a_reserved_glyph_an_author_wrote_is_the_one_thing_reallocated(self) -> None:
        legend = allocate_legend(["wall", "water"], prefer={"+": "wall", "~": "water"})

        assert legend["~"] == "water"
        assert "+" not in legend
        assert "wall" in legend.values()

    def test_a_preference_for_a_kind_that_is_not_in_play_is_simply_unused(self) -> None:
        legend = allocate_legend(["wall"], prefer={"~": "water", "W": "wall"})

        assert legend == {"W": "wall"}

    def test_the_shared_default_legend_is_the_next_preference_after_the_author(
        self,
    ) -> None:
        # Readability, and consistency with a generated map: a document written
        # for a fight should spell floor '.' and wall '#' like every other one.
        legend = allocate_legend(["floor", "wall"])

        assert legend == {".": "floor", "#": "wall"}
        assert all(DEFAULT_LEGEND[glyph] == kind for glyph, kind in legend.items())


class TestMapDocumentFlat:
    """``MapDocument.flat``: the document twin of ``BattleMap.flat``.

    One plane, built from what a caller already has — dimensions, a default
    kind, the squares that differ from it — and the format's own concerns
    (glyphs, dense tiles, provenance) filled in. The property every case here
    turns on is that the result is a document the parser accepts: a builder that
    can produce one it does not would put an unrecoverable map in a journal,
    which is the exact defect an inline spec's missing door orientation caused.
    """

    @staticmethod
    def _parsed(document_object: MapDocument) -> MapDocument:
        return parse_document(as_payload(document_object), source="flat", terrain=TERRAIN)

    def test_a_sparse_terrain_mapping_densifies_into_rows(self) -> None:
        built = MapDocument.flat(
            name="strip", width=4, height=2,
            default_terrain="floor", terrain={(1, 0): "wall", (3, 1): "water"},
        )

        glyph = {kind: char for char, kind in built.legend.items()}
        assert built.tiles == (
            f"{glyph['floor']}{glyph['wall']}{glyph['floor']}{glyph['floor']}",
            f"{glyph['floor']}{glyph['floor']}{glyph['floor']}{glyph['water']}",
        )

    def test_what_it_builds_is_a_document_the_parser_accepts(self) -> None:
        built = MapDocument.flat(
            name="strip", width=4, height=2,
            default_terrain="floor", terrain={(1, 0): "wall"},
        )

        assert as_payload(self._parsed(built)) == as_payload(built)

    def test_it_answers_the_same_square_by_square_as_the_battle_map_twin(self) -> None:
        # The two ``flat`` constructors are one shape authored twice; if they
        # disagreed about what a square is, collapsing map production onto the
        # document would silently move a fight's terrain.
        terrain = {(1, 0): "wall", (3, 1): "water", (0, 1): "difficult"}
        built = MapDocument.flat(
            name="strip", width=4, height=2, default_terrain="floor", terrain=terrain
        )
        twin = BattleMap.flat(
            name="strip", width=4, height=2, default_terrain="floor", terrain=terrain
        )

        bridged = to_grid(built)
        for y in range(2):
            for x in range(4):
                assert bridged.ground.terrain.get(
                    (x, y), bridged.default_terrain
                ) == twin.ground.terrain.get((x, y), twin.default_terrain)

    def test_a_reserved_glyph_in_the_authors_legend_moves_and_the_tiles_follow(
        self,
    ) -> None:
        # The tiles are written from the allocation, never from the preference,
        # so a reallocated glyph cannot leave a row pointing at a legend entry
        # that is no longer there.
        built = MapDocument.flat(
            name="strip", width=2, height=1,
            default_terrain="floor", terrain={(1, 0): "wall"},
            legend={"@": "wall", ".": "floor"},
        )

        assert "@" not in built.tiles[0]
        assert self._parsed(built).tiles == built.tiles

    def test_features_cross_whole_and_survive_the_round_trip(self) -> None:
        built = MapDocument.flat(
            name="hall", width=3, height=1, default_terrain="floor",
            features=(
                MapFeatureRecord(
                    id="lever", kind="lever", at=(0, 0), state="closed",
                    terrain=TerrainPair(closed="floor", open="floor"),
                ),
                MapFeatureRecord(
                    id="gate", kind="gate", at=(2, 0), state="closed",
                    terrain=TerrainPair(closed="floor", open="floor"),
                    trigger=FeatureTrigger(
                        when=(("lever", True),), set_open=True,
                        mode=TriggerMode.MAINTAINED,
                    ),
                ),
            ),
        )

        gate = next(one for one in self._parsed(built).features if one.id == "gate")
        assert gate.trigger == FeatureTrigger(
            when=(("lever", True),), set_open=True, mode=TriggerMode.MAINTAINED
        )

    def test_heights_cross_as_a_datum_and_the_squares_that_depart_from_it(self) -> None:
        built = MapDocument.flat(
            name="ledge", width=3, height=1, default_terrain="floor",
            default_elevation=10, elevation={(2, 0): 25},
        )

        assert built.elevation == MapElevation(default=10, squares={(2, 0): 25})
        assert self._parsed(built).elevation.at((2, 0)) == 25

    def test_a_square_outside_the_grid_is_not_drawn_and_not_recorded(self) -> None:
        # ``BattleMap.flat`` never consults such a square; a document would write
        # it out and the parser would refuse the file. Dropped, so the builder
        # cannot make a document nothing can read.
        built = MapDocument.flat(
            name="strip", width=2, height=1, default_terrain="floor",
            terrain={(9, 9): "wall"}, elevation={(9, 9): 30},
        )

        assert "wall" not in built.legend.values()
        assert built.elevation.squares == {}
        assert as_payload(self._parsed(built)) == as_payload(built)

    def test_the_default_provenance_is_one_the_format_accepts(self) -> None:
        built = MapDocument.flat(name="strip", width=1, height=1)

        assert built.provenance.source
        assert as_payload(self._parsed(built))["provenance"] == as_payload(built)[
            "provenance"
        ]
