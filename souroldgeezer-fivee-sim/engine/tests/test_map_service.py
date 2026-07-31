"""The transport-neutral map service: generate, edit, render, query, store.

The properties pinned here are the ones both adapters lean on: generation is
deterministic under a seed, editing is atomic and flips ``edited`` only on a
real change, rendering honours its viewport and cell budget, and storage
round-trips byte-identically while refusing silent overwrites.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.kernel.grid import TERRAIN
from fivee_sim.maps import MapDocument, MapError, parse_document, serialize
from fivee_sim.service import maps as service
from fivee_sim.service.common import resolve_seed, sha256_of, slugify
from fivee_sim.service.errors import MapEditError


def payload() -> dict[str, Any]:
    """A small valid document, rebuilt fresh so tests may mutate freely."""
    return {
        "format": "fivee-sim-map",
        "format_version": 1,
        "name": "service-chamber",
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


def document() -> MapDocument:
    return parse_document(payload(), source="test", terrain=TERRAIN)


def edited(doc: MapDocument, *operations: dict[str, Any]) -> MapDocument:
    return service.apply_edits(doc, list(operations), terrain=TERRAIN)


class TestCommon:
    def test_resolve_seed_passes_a_given_seed_through(self) -> None:
        assert resolve_seed(42) == 42
        assert resolve_seed(0) == 0

    def test_resolve_seed_invents_one_only_when_absent(self) -> None:
        assert isinstance(resolve_seed(None), int)

    def test_slugify_is_filesystem_safe(self) -> None:
        assert slugify("Dungeon 42!") == "dungeon-42"
        assert slugify("  ///  ") == "map"

    def test_sha256_is_the_text_digest(self) -> None:
        assert sha256_of("abc").startswith("ba7816bf")


class TestGenerate:
    def test_the_same_seed_and_params_reproduce_identical_bytes(self) -> None:
        first = service.generate("dungeon", {"width": 24, "height": 20}, 11)
        second = service.generate("dungeon", {"width": 24, "height": 20}, 11)
        assert serialize(first) == serialize(second)

    def test_a_different_seed_produces_a_different_map(self) -> None:
        first = service.generate("caves", None, 1)
        second = service.generate("caves", None, 2)
        assert serialize(first) != serialize(second)

    def test_every_kind_generates_and_names_itself_after_kind_and_seed(self) -> None:
        for kind in ("dungeon", "caves", "overland"):
            doc = service.generate(kind, None, 5)
            assert doc.name == f"{kind} 5"
            assert doc.provenance.generator == kind
            assert doc.provenance.seed == 5
            assert doc.provenance.edited is False

    def test_params_are_recorded_fully_resolved(self) -> None:
        doc = service.generate("caves", {"width": 30}, 3, name="deep dark")
        assert doc.name == "deep dark"
        params = dict(doc.provenance.params)
        assert params["width"] == 30
        # Defaults ride along, so the document alone reproduces the map.
        assert params["initial_wall_chance"] == 0.45
        assert params["smoothing_passes"] == 5

    def test_an_unknown_kind_lists_the_valid_ones(self) -> None:
        with pytest.raises(ValueError, match="caves, dungeon, overland"):
            service.generate("labyrinth", None, 1)

    def test_an_unknown_param_names_the_valid_keys(self) -> None:
        with pytest.raises(ValueError, match="'rooms'.*min_room"):
            service.generate("dungeon", {"rooms": 4}, 1)

    def test_a_mistyped_param_value_is_refused(self) -> None:
        with pytest.raises(ValueError, match="whole number"):
            service.generate("dungeon", {"width": "wide"}, 1)
        with pytest.raises(ValueError, match="number"):
            service.generate("overland", {"scale": "big"}, 1)

    def test_a_bad_param_value_is_the_generator_refusing(self) -> None:
        with pytest.raises(ValueError, match="door_chance"):
            service.generate("dungeon", {"door_chance": 2.0}, 1)


class TestEditOps:
    def test_set_terrain_fills_a_rectangle(self) -> None:
        doc = edited(document(), {"op": "set_terrain", "rect": [1, 1, 2, 2], "terrain": "wall"})
        assert doc.tiles[1] == "###..#"
        assert doc.tiles[2] == "###..#"

    def test_paint_touches_exactly_the_named_cells(self) -> None:
        doc = edited(
            document(), {"op": "paint", "cells": [[1, 1], [4, 3]], "terrain": "difficult"}
        )
        assert doc.tiles[1] == "#%...#"
        assert doc.tiles[3] == "#...%#"

    def test_line_draws_a_bresenham_raster(self) -> None:
        doc = edited(document(), {"op": "line", "from": [1, 1], "to": [4, 3], "terrain": "wall"})
        assert doc.tiles[1] == "##...#"
        assert doc.tiles[2] == "#.##.#"  # x=2 keeps midline; x=3 rounds down
        assert doc.tiles[3] == "#...##"

    def test_carve_corridor_bends_horizontally_first_by_default(self) -> None:
        doc = edited(
            document(), {"op": "carve_corridor", "from": [1, 1], "to": [4, 3],
                         "terrain": "difficult"}
        )
        assert doc.tiles[1] == "#%%%%#"
        assert doc.tiles[2] == "#.%.%#"
        assert doc.tiles[3] == "#...%#"

    def test_carve_corridor_bends_vertically_first_when_asked(self) -> None:
        doc = edited(
            document(), {"op": "carve_corridor", "from": [1, 1], "to": [4, 3],
                         "terrain": "difficult", "horizontal_first": False}
        )
        assert doc.tiles[1] == "#%...#"
        assert doc.tiles[2] == "#%%..#"  # the vertical leg, plus the pre-existing patch
        assert doc.tiles[3] == "#%%%%#"

    def test_carve_corridor_defaults_to_floor(self) -> None:
        doc = edited(document(), {"op": "carve_corridor", "from": [2, 2], "to": [2, 2]})
        assert doc.tiles[2] == "#....#"

    def test_add_and_remove_feature(self) -> None:
        doc = edited(
            document(),
            {"op": "add_feature", "feature": {
                "id": "door-2", "kind": "door", "at": [4, 1],
                "orientation": "vertical", "state": "open",
            }},
        )
        assert [f.id for f in doc.features] == ["door-1", "spawn-party", "door-2"]
        trimmed = edited(doc, {"op": "remove_feature", "id": "spawn-party"})
        assert [f.id for f in trimmed.features] == ["door-1", "door-2"]

    def test_a_door_needs_orientation_and_state(self) -> None:
        with pytest.raises(MapEditError, match="orientation"):
            edited(document(), {"op": "add_feature",
                                "feature": {"id": "d", "kind": "door", "at": [4, 1]}})

    def test_a_duplicate_feature_id_is_refused(self) -> None:
        with pytest.raises(MapEditError, match="'door-1' already exists"):
            edited(document(), {"op": "add_feature", "feature": {
                "id": "door-1", "kind": "spawn", "at": [4, 1]}})

    def test_removing_a_missing_feature_lists_what_exists(self) -> None:
        with pytest.raises(MapEditError, match="door-1.*spawn-party"):
            edited(document(), {"op": "remove_feature", "id": "portcullis"})

    def test_toggle_door_flips_the_recorded_default(self) -> None:
        doc = edited(document(), {"op": "toggle_door", "at": [3, 4]})
        assert doc.features[0].state == "open"
        again = edited(doc, {"op": "toggle_door", "at": [3, 4]})
        assert again.features[0].state == "closed"

    def test_toggle_door_without_a_door_names_the_doors(self) -> None:
        with pytest.raises(MapEditError, match=r"no door at \[1, 1\].*\[3, 4\]"):
            edited(document(), {"op": "toggle_door", "at": [1, 1]})

    def test_resize_grows_with_fill_and_keeps_the_anchor_corner(self) -> None:
        doc = edited(document(), {"op": "resize", "width": 8, "height": 6})
        assert doc.grid.width == 8 and doc.grid.height == 6
        assert doc.tiles[1] == "#....###"  # old content at top-left, wall fill beyond
        assert doc.tiles[5] == "########"
        assert doc.features[0].at == (3, 4)  # unmoved under the default anchor

    def test_resize_anchored_bottom_right_shifts_content_and_features(self) -> None:
        doc = edited(
            document(), {"op": "resize", "width": 8, "height": 6, "anchor": "bottom-right"}
        )
        assert doc.tiles[0] == "########"
        assert doc.tiles[2] == "###....#"  # old row 1, shifted right by 2, down by 1
        assert doc.features[0].at == (5, 5)

    def test_resize_smaller_drops_features_left_outside(self) -> None:
        doc = edited(document(), {"op": "resize", "width": 6, "height": 4})
        assert [f.id for f in doc.features] == ["spawn-party"]  # the door at y=4 fell off

    def test_set_legend_admits_a_new_kind_for_painting(self) -> None:
        doc = edited(
            document(),
            {"op": "set_legend", "glyph": "~", "terrain": "water"},
            {"op": "paint", "cells": [[2, 3]], "terrain": "water"},
        )
        assert doc.legend["~"] == "water"
        assert doc.tiles[3] == "#.~..#"

    def test_a_reserved_glyph_is_refused(self) -> None:
        with pytest.raises(MapEditError, match="reserved"):
            edited(document(), {"op": "set_legend", "glyph": "+", "terrain": "floor"})

    def test_an_unknown_terrain_kind_is_refused_with_the_loaded_list(self) -> None:
        with pytest.raises(MapEditError, match="'lava'.*Available:"):
            edited(document(), {"op": "set_legend", "glyph": "!", "terrain": "lava"})

    def test_painting_a_kind_without_a_glyph_points_at_set_legend(self) -> None:
        with pytest.raises(MapEditError, match="no glyph.*set_legend"):
            edited(document(), {"op": "paint", "cells": [[1, 1]], "terrain": "water"})

    def test_set_name_renames_and_marks_the_document_edited(self) -> None:
        doc = edited(document(), {"op": "set_name", "name": "the renamed chamber"})
        assert doc.name == "the renamed chamber"
        assert doc.provenance.edited is True


class TestElevationEdits:
    def raised(self) -> MapDocument:
        """The room with a 20-foot plateau across its middle column."""
        return edited(
            document(),
            {"op": "set_elevation", "rect": [3, 1, 2, 3], "feet": 20},
        )

    def test_set_elevation_raises_a_rect(self) -> None:
        doc = self.raised()
        assert doc.elevation.at((3, 1)) == 20
        assert doc.elevation.at((4, 3)) == 20
        assert doc.elevation.at((1, 1)) == 0

    def test_set_elevation_takes_named_cells_too(self) -> None:
        doc = edited(document(), {"op": "set_elevation", "cells": [[1, 1], [2, 2]], "feet": -5})
        assert doc.elevation.at((1, 1)) == -5
        assert doc.elevation.at((2, 2)) == -5
        assert doc.elevation.at((3, 3)) == 0

    def test_adjust_elevation_is_relative_to_what_is_there(self) -> None:
        doc = edited(
            self.raised(),
            {"op": "adjust_elevation", "rect": [3, 1, 2, 1], "by": 10},
        )
        assert doc.elevation.at((3, 1)) == 30
        assert doc.elevation.at((3, 2)) == 20

    def test_the_default_moves_the_ground_every_unnamed_square_stands_on(self) -> None:
        doc = edited(self.raised(), {"op": "set_elevation", "default": 20})
        assert doc.elevation.default == 20
        assert doc.elevation.at((1, 1)) == 20
        # The plateau matched the new datum, so it is no longer worth recording.
        assert dict(doc.elevation.squares) == {}

    def test_a_square_set_back_to_the_default_leaves_the_layer(self) -> None:
        doc = edited(self.raised(), {"op": "set_elevation", "rect": [3, 1, 2, 3], "feet": 0})
        assert dict(doc.elevation.squares) == {}
        assert "elevation" not in json.loads(serialize(doc))

    def test_an_unrelated_edit_keeps_the_height_layer(self) -> None:
        # The trap: apply_edits rebuilds the whole payload, so a layer the edit
        # state forgets is one every unrelated edit silently flattens.
        doc = edited(self.raised(), {"op": "set_name", "name": "renamed"})
        assert doc.name == "renamed"
        assert doc.elevation.at((3, 1)) == 20

    def test_resize_moves_the_height_with_the_anchor(self) -> None:
        doc = edited(
            self.raised(),
            {"op": "resize", "width": 8, "height": 5, "anchor": "top-right"},
        )
        # Anchored top-right, everything shifts two squares east.
        assert doc.elevation.at((5, 1)) == 20
        assert doc.elevation.at((3, 1)) == 0

    def test_resize_drops_height_that_falls_off_the_map(self) -> None:
        doc = edited(self.raised(), {"op": "resize", "width": 3, "height": 5})
        assert dict(doc.elevation.squares) == {}

    def test_naming_both_a_rect_and_cells_is_refused(self) -> None:
        with pytest.raises(MapEditError, match="exactly one of 'rect'"):
            edited(document(), {"op": "set_elevation", "rect": [0, 0, 1, 1],
                                "cells": [[1, 1]], "feet": 5})

    def test_naming_neither_a_rect_nor_cells_is_refused(self) -> None:
        with pytest.raises(MapEditError, match="exactly one of 'rect'"):
            edited(document(), {"op": "adjust_elevation", "by": 5})

    def test_the_default_cannot_be_combined_with_a_target(self) -> None:
        with pytest.raises(MapEditError, match="cannot be combined"):
            edited(document(), {"op": "set_elevation", "default": 5, "rect": [0, 0, 1, 1]})

    def test_a_non_integer_height_is_refused(self) -> None:
        with pytest.raises(MapEditError, match="whole number of feet"):
            edited(document(), {"op": "set_elevation", "rect": [0, 0, 1, 1], "feet": "high"})

    def test_a_rect_off_the_map_is_refused(self) -> None:
        with pytest.raises(MapEditError, match="reaches outside"):
            edited(document(), {"op": "set_elevation", "rect": [4, 4, 9, 9], "feet": 5})


class TestEditAtomicity:
    def test_a_bad_op_names_its_index_and_applies_nothing(self) -> None:
        before = document()
        frozen = serialize(before)
        with pytest.raises(MapEditError) as caught:
            service.apply_edits(
                before,
                [
                    {"op": "paint", "cells": [[1, 1]], "terrain": "wall"},
                    {"op": "set_name", "name": "halfway"},
                    {"op": "paint", "cells": [[99, 99]], "terrain": "wall"},
                ],
                terrain=TERRAIN,
            )
        assert caught.value.op_index == 2
        assert "operation #2" in str(caught.value)
        assert serialize(before) == frozen  # the input document is untouched

    def test_an_unknown_op_lists_the_valid_ones(self) -> None:
        with pytest.raises(MapEditError, match="carve_corridor.*toggle_door"):
            edited(document(), {"op": "sculpt"})

    def test_an_unknown_op_key_is_refused(self) -> None:
        with pytest.raises(MapEditError, match="unknown key.*'colour'"):
            edited(document(), {"op": "set_name", "name": "x", "colour": "red"})

    def test_provenance_generation_lineage_survives_an_edit(self) -> None:
        doc = edited(document(), {"op": "paint", "cells": [[1, 1]], "terrain": "wall"})
        assert doc.provenance.generator == "hand"
        assert doc.provenance.seed == 7
        assert dict(doc.provenance.params) == {"width": 6, "height": 5}
        assert doc.provenance.edited is True

    def test_a_no_op_edit_returns_the_same_document_unedited(self) -> None:
        before = document()
        after = service.apply_edits(
            before,
            [{"op": "paint", "cells": [[1, 1]], "terrain": "floor"}],  # already floor
            terrain=TERRAIN,
        )
        assert after is before
        assert after.provenance.edited is False


class TestRenderAscii:
    def test_the_whole_map_renders_through_the_document_legend(self) -> None:
        rendered = service.render_ascii(document(), show_features=False)
        assert rendered["rows"] == list(payload()["tiles"])
        assert rendered["legend"] == {".": "floor", "#": "wall", "%": "difficult"}
        assert rendered["truncated"] is False
        assert rendered["viewport"] == {
            "x": 0, "y": 0, "width": 6, "height": 5, "downsample": 1,
        }

    def test_features_overlay_with_their_reserved_glyphs(self) -> None:
        rendered = service.render_ascii(document())
        assert rendered["rows"][4] == "###+##"  # the closed door
        assert rendered["rows"][1] == "#@...#"  # the spawn hint
        opened = edited(document(), {"op": "toggle_door", "at": [3, 4]})
        assert service.render_ascii(opened)["rows"][4] == "###/##"

    def test_tokens_outrank_features_which_outrank_terrain(self) -> None:
        rendered = service.render_ascii(
            document(), tokens={(1, 1): "A", (2, 2): "B"}
        )
        assert rendered["rows"][1] == "#A...#"  # token beats the spawn overlay
        assert rendered["rows"][2] == "#.B..#"  # token beats terrain

    def test_a_viewport_clamps_and_reports_truncation(self) -> None:
        rendered = service.render_ascii(
            document(), x=1, y=1, width=99, height=2, show_features=False
        )
        assert rendered["rows"] == ["....#", ".%..#"]
        assert rendered["viewport"]["width"] == 5
        assert rendered["truncated"] is True

    def test_downsample_renders_the_majority_kind(self) -> None:
        rendered = service.render_ascii(document(), downsample=3, show_features=False)
        # Blocks of 3x3: top-left is 5 wall / 3 floor / 1 difficult -> wall.
        assert rendered["rows"][0] == "##"
        assert rendered["viewport"]["downsample"] == 3

    def test_height_is_not_rendered_unless_it_is_asked_for(self) -> None:
        rendered = service.render_ascii(document())
        assert "elevation_rows" not in rendered
        assert "elevation_legend" not in rendered

    def test_height_renders_as_a_contour_beside_the_terrain(self) -> None:
        raised = edited(
            document(),
            {"op": "set_elevation", "rect": [3, 1, 2, 2], "feet": 20},
            {"op": "set_elevation", "cells": [[1, 1]], "feet": -10},
        )
        rendered = service.render_ascii(raised, show_elevation=True, show_features=False)
        assert rendered["rows"] == list(payload()["tiles"])  # terrain is untouched
        assert rendered["elevation_rows"] == [
            "111111",
            "101221",
            "111221",
            "111111",
            "111111",
        ]
        assert rendered["elevation_legend"] == {"0": -10, "1": 0, "2": 20}

    def test_a_downsampled_block_takes_its_majority_height(self) -> None:
        raised = edited(document(), {"op": "set_elevation", "rect": [0, 0, 3, 3], "feet": 20})
        rendered = service.render_ascii(raised, downsample=3, show_elevation=True)
        # The top-left 3x3 block is all plateau; the rest falls back to the datum,
        # and ties within a block go to the lower ground.
        assert rendered["elevation_rows"] == ["10", "00"]
        assert rendered["elevation_legend"] == {"0": 0, "1": 20}

    def test_more_heights_than_glyphs_is_refused_with_the_remedy(self) -> None:
        terraced = parse_document(
            {
                **payload(),
                "grid": {"width": 8, "height": 6, "cell_feet": 5},
                "tiles": ["........"] * 6,
                "elevation": {
                    "default": 0,
                    "squares": [[x, y, 5 * (y * 8 + x)] for y in range(6) for x in range(8)],
                },
                "features": [],
            },
            source="terraced",
            terrain=TERRAIN,
        )
        with pytest.raises(ValueError, match="raise downsample"):
            service.render_ascii(terraced, show_elevation=True)

    def test_a_downsample_tie_falls_to_legend_order(self) -> None:
        two = parse_document(
            {
                **payload(),
                "grid": {"width": 2, "height": 1, "cell_feet": 5},
                "tiles": [".#"],
                "features": [],
            },
            source="tie",
            terrain=TERRAIN,
        )
        rendered = service.render_ascii(two, downsample=2)
        # One floor, one wall: the parsed legend sorts by glyph, and '#' (wall)
        # sorts before '.' (floor), so the tie falls to the wall.
        assert rendered["rows"] == ["#"]

    def test_features_in_view_lists_only_whats_visible(self) -> None:
        rendered = service.render_ascii(document(), x=0, y=0, width=6, height=3)
        assert [entry["id"] for entry in rendered["features_in_view"]] == ["spawn-party"]

    def test_the_cell_budget_refuses_with_the_remedy(self) -> None:
        big = parse_document(
            {
                **payload(),
                "grid": {"width": 200, "height": 200, "cell_feet": 5},
                "tiles": ["." * 200] * 200,
                "features": [],
            },
            source="big",
            terrain=TERRAIN,
        )
        with pytest.raises(ValueError, match="10000-cell budget.*downsample"):
            service.render_ascii(big)
        # The same map fits once downsampled.
        assert len(service.render_ascii(big, downsample=2)["rows"]) == 100

    def test_a_bad_downsample_is_refused(self) -> None:
        with pytest.raises(ValueError, match="downsample"):
            service.render_ascii(document(), downsample=0)


class TestQuery:
    def wall_split(self, door_state: str | None = None) -> MapDocument:
        """A 5x4 room split by a wall; optionally a door in the bottom gap."""
        features = []
        if door_state is not None:
            features.append({
                "id": "door-1", "kind": "door", "at": [2, 3],
                "orientation": "vertical", "state": door_state,
            })
        return parse_document(
            {
                **payload(),
                "name": "split",
                "grid": {"width": 5, "height": 4, "cell_feet": 5},
                "tiles": ["..#..", "..#..", "..#..", "....."],
                "features": features,
            },
            source="split",
            terrain=TERRAIN,
        )

    def test_distance_is_in_feet_between_square_centres(self) -> None:
        result = service.query(self.wall_split(), "distance", (0, 1), (4, 1), terrain=TERRAIN)
        assert result["feet"] == 20

    def test_sight_is_blocked_by_the_wall_and_clear_along_the_gap(self) -> None:
        doc = self.wall_split()
        blocked = service.query(doc, "line_of_sight", (0, 1), (4, 1), terrain=TERRAIN)
        assert blocked["line_of_sight"] is False
        clear = service.query(doc, "line_of_sight", (0, 3), (4, 3), terrain=TERRAIN)
        assert clear["line_of_sight"] is True

    def test_a_path_routes_around_the_wall_with_its_cost(self) -> None:
        result = service.query(self.wall_split(), "path", (0, 1), (4, 1), terrain=TERRAIN)
        assert result["reachable"] is True
        assert result["cost_feet"] == 20
        assert (2, 3) in {tuple(square) for square in result["squares"]}

    def test_a_closed_door_blocks_and_an_open_one_admits(self) -> None:
        shut = service.query(self.wall_split("closed"), "path", (0, 1), (4, 1), terrain=TERRAIN)
        assert shut["reachable"] is False
        ajar = service.query(self.wall_split("open"), "path", (0, 1), (4, 1), terrain=TERRAIN)
        assert ajar["reachable"] is True

    def test_an_unknown_query_lists_the_valid_ones(self) -> None:
        with pytest.raises(ValueError, match="distance, line_of_sight, path"):
            service.query(self.wall_split(), "cover", (0, 0), (1, 1), terrain=TERRAIN)

    def test_an_off_map_square_is_refused(self) -> None:
        with pytest.raises(ValueError, match="outside the 5x4 map"):
            service.query(self.wall_split(), "distance", (0, 0), (9, 9), terrain=TERRAIN)

    def stepped(self) -> MapDocument:
        """A one-row corridor with a 20-foot cliff at its far end."""
        return parse_document(
            {
                **payload(),
                "name": "ledge",
                "grid": {"width": 4, "height": 1, "cell_feet": 5},
                "tiles": ["...."],
                "elevation": {"default": 0, "squares": [[3, 0, 20]]},
                "features": [],
            },
            source="ledge",
            terrain=TERRAIN,
        )

    def test_a_path_pays_for_the_climb(self) -> None:
        result = service.query(self.stepped(), "path", (0, 0), (3, 0), terrain=TERRAIN)
        assert result["reachable"] is True
        assert result["cost_feet"] == 5 + 5 + (5 + 40)

    def test_a_path_reports_the_ground_at_both_ends(self) -> None:
        result = service.query(self.stepped(), "path", (0, 0), (3, 0), terrain=TERRAIN)
        assert (result["from_elevation"], result["to_elevation"]) == (0, 20)

    def test_distance_and_sight_stay_flat(self) -> None:
        doc = self.stepped()
        assert service.query(doc, "distance", (0, 0), (3, 0), terrain=TERRAIN)["feet"] == 15
        sight = service.query(doc, "line_of_sight", (0, 0), (3, 0), terrain=TERRAIN)
        assert sight["line_of_sight"] is True


class TestFiles:
    def test_save_then_load_round_trips_byte_identically(self, tmp_path: Path) -> None:
        doc = document()
        saved = service.save_file(doc, tmp_path / "chamber.json")
        loaded, warnings = service.load_file(saved["path"], terrain=TERRAIN)
        assert serialize(loaded) == serialize(doc)
        assert warnings == []
        assert saved["sha256"] == sha256_of(serialize(doc))
        assert saved["bytes"] == len(serialize(doc).encode("utf-8"))

    def test_save_refuses_a_silent_overwrite(self, tmp_path: Path) -> None:
        target = tmp_path / "chamber.json"
        service.save_file(document(), target)
        with pytest.raises(ValueError, match="already exists.*overwrite"):
            service.save_file(document(), target)
        # Deliberate replacement still works.
        service.save_file(document(), target, overwrite=True)

    def test_loading_a_missing_file_is_a_located_map_error(self, tmp_path: Path) -> None:
        with pytest.raises(MapError, match="cannot be read"):
            service.load_file(tmp_path / "nowhere.json", terrain=TERRAIN)

    def test_loading_junk_json_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "junk.json"
        target.write_text("{not json", encoding="utf-8")
        with pytest.raises(MapError, match="not valid JSON"):
            service.load_file(target, terrain=TERRAIN)

    def test_loading_an_invalid_document_carries_every_diagnostic(
        self, tmp_path: Path
    ) -> None:
        broken = payload()
        broken["tiles"][1] = "#..?.#"
        broken["features"][1]["id"] = "door-1"
        target = tmp_path / "broken.json"
        target.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(MapError) as caught:
            service.load_file(target, terrain=TERRAIN)
        assert len(caught.value.diagnostics) == 2

    def test_maps_root_prefers_the_environment_then_the_project(self) -> None:
        assert service.maps_root({"FIVEE_SIM_MAPS": "/somewhere/maps"}) == Path(
            "/somewhere/maps"
        )
        assert service.maps_root({"CLAUDE_PROJECT_DIR": "/repo"}) == Path(
            "/repo/.fivee-sim/maps"
        )
        assert service.maps_root({}) == Path.cwd() / ".fivee-sim" / "maps"

    def test_environment_roots_split_on_the_path_separator(self) -> None:
        roots = service.environment_roots(
            {"FIVEE_SIM_MAPS": os.pathsep.join(["/a", "", "/b"])}
        )
        assert roots == ["/a", "/b"]

    def test_list_maps_catalogues_documents_and_skips_everything_else(
        self, tmp_path: Path
    ) -> None:
        service.save_file(document(), tmp_path / "b-chamber.json")
        other = service.generate("caves", {"width": 12, "height": 10}, 3)
        service.save_file(other, tmp_path / "a-caves.json")
        (tmp_path / "not-a-map.json").write_text('{"pack": "x"}', encoding="utf-8")
        (tmp_path / "junk.json").write_text("{", encoding="utf-8")

        listed = service.list_maps([tmp_path])
        assert [entry["name"] for entry in listed] == ["caves 3", "service-chamber"]
        first = listed[0]
        assert first["width"] == 12 and first["height"] == 10
        assert first["generator"] == "caves"
        assert first["edited"] is False
        assert first["path"].endswith("a-caves.json")
