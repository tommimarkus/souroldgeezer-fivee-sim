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

from fivee_sim.kernel.grid import TERRAIN, Square
from fivee_sim.map_document import (
    RESERVED_GLYPHS,
    MapColor,
    MapDocument,
    MapError,
    parse_document,
    serialize,
    to_grid,
)
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


def linked_document() -> MapDocument:
    raw = payload()
    raw["features"] = [
        {
            "id": "door-1",
            "kind": "door",
            "at": [3, 4],
            "orientation": "horizontal",
            "hinge": "east",
            "swing": "north",
            "state": "closed",
            "linked_to": "door-2",
        },
        {
            "id": "door-2",
            "kind": "door",
            "at": [2, 4],
            "orientation": "horizontal",
            "hinge": "west",
            "swing": "north",
            "state": "closed",
            "linked_to": "door-1",
        },
        {"id": "spawn-party", "kind": "spawn", "at": [1, 1], "team": "party"},
    ]
    return parse_document(raw, source="test", terrain=TERRAIN)


def edited(doc: MapDocument, *operations: dict[str, Any]) -> MapDocument:
    return service.apply_edits(doc, list(operations), terrain=TERRAIN)


class TestCommon:
    def test_resolve_seed_passes_a_given_seed_through(self) -> None:
        assert resolve_seed(42) == 42
        assert resolve_seed(0) == 0

    def test_resolve_seed_invents_one_only_when_absent(self) -> None:
        assert isinstance(resolve_seed(None), int)

    @pytest.mark.parametrize("seed", [-(2**53), 2**53])
    def test_resolve_seed_rejects_values_javascript_cannot_reproduce(
        self, seed: int
    ) -> None:
        with pytest.raises(ValueError, match="JavaScript safe integer"):
            resolve_seed(seed)

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

    def test_toggle_door_flips_both_linked_defaults(self) -> None:
        doc = edited(linked_document(), {"op": "toggle_door", "at": [3, 4]})
        assert {
            feature.id: feature.state for feature in doc.features if feature.kind == "door"
        } == {
            "door-1": "open",
            "door-2": "open",
        }

    def test_removing_one_linked_leaf_is_refused_at_the_operation(self) -> None:
        with pytest.raises(MapEditError, match="linked to 'door-2'.*unlink"):
            edited(linked_document(), {"op": "remove_feature", "id": "door-1"})

    def test_resize_cannot_drop_only_one_linked_leaf(self) -> None:
        with pytest.raises(MapEditError, match="push 'door-1' off.*'door-2' is linked"):
            edited(
                linked_document(),
                {"op": "resize", "width": 3, "height": 5, "anchor": "top-left"},
            )

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


def storeyed() -> MapDocument:
    """The base document with a gallery over it, reached by a stair at (3, 3)."""
    raw = payload()
    # The doorway stands open, so the ground's one gap in the bottom wall is
    # walkable and the gallery's solid row above it is the only thing blocking.
    raw["features"][0]["state"] = "open"
    raw["features"].append(
        {"id": "stair-foot", "kind": "stairs_up", "at": [3, 3], "to_level": 1}
    )
    raw["levels"] = [
        {
            "index": 1,
            "name": "gallery",
            "tiles": ["######", "#....#", "#....#", "#....#", "######"],
            "elevation": {"default": 10, "squares": []},
            "features": [
                {"id": "stair-head", "kind": "stairs_down", "at": [3, 3], "to_level": 0}
            ],
        }
    ]
    return parse_document(raw, source="storeyed", terrain=TERRAIN)


class TestLevelEdits:
    def test_an_edit_to_the_ground_leaves_the_storey_alone(self) -> None:
        # The layer-left-unwired failure this format was designed against: the
        # edit state rebuilds the whole document, so a storey it does not carry
        # is a storey every unrelated edit deletes.
        doc = storeyed()
        after = edited(doc, {"op": "set_terrain", "rect": [1, 1, 2, 1], "terrain": "wall"})
        assert set(after.levels) == {0, 1}
        assert after.levels[1].tiles == doc.levels[1].tiles
        assert after.levels[1].elevation.default == 10
        assert [f.id for f in after.levels[1].features] == ["stair-head"]

    def test_an_op_paints_the_level_it_names(self) -> None:
        doc = storeyed()
        after = edited(
            doc, {"op": "set_terrain", "rect": [1, 1, 2, 1], "terrain": "wall", "level": 1}
        )
        assert after.levels[1].tiles[1] == "###..#"
        assert after.levels[0].tiles == doc.levels[0].tiles

    def test_an_op_without_a_level_means_the_ground(self) -> None:
        doc = storeyed()
        after = edited(doc, {"op": "set_terrain", "rect": [1, 1, 2, 1], "terrain": "wall"})
        assert after.levels[0].tiles[1] == "###..#"
        assert after.levels[1].tiles == doc.levels[1].tiles

    def test_heights_are_painted_on_the_level_they_name(self) -> None:
        doc = storeyed()
        after = edited(
            doc, {"op": "set_elevation", "cells": [[1, 1]], "feet": 25, "level": 1}
        )
        assert after.levels[1].elevation.at((1, 1)) == 25
        assert after.levels[1].elevation.at((2, 1)) == 10  # the storey's own datum
        assert after.levels[0].elevation.at((1, 1)) == 0

    def test_a_feature_is_added_to_the_level_it_names(self) -> None:
        doc = storeyed()
        after = edited(
            doc,
            {
                "op": "add_feature",
                "level": 1,
                "feature": {
                    "id": "gallery-door", "kind": "door", "at": [1, 1],
                    "orientation": "horizontal", "state": "closed",
                },
            },
        )
        assert [f.id for f in after.levels[1].features] == ["stair-head", "gallery-door"]
        assert [f.id for f in after.levels[0].features] == [
            "door-1", "spawn-party", "stair-foot",
        ]

    def test_resizing_moves_every_storey(self) -> None:
        # A frame change is document-wide: a resize that translated only the
        # ground would leave the storeys mislocated over it.
        doc = storeyed()
        after = edited(doc, {"op": "resize", "width": 8, "height": 5, "anchor": "top-right"})
        assert after.grid.width == 8
        assert len(after.levels[1].tiles) == 5
        assert all(len(row) == 8 for row in after.levels[1].tiles)
        assert after.levels[1].tiles[1] == "##" + "#....#"
        head = next(f for f in after.levels[1].features if f.id == "stair-head")
        assert head.at == (5, 3)

    def test_a_feature_id_already_used_on_another_level_is_refused(self) -> None:
        # Ids are unique document-wide, so the refusal has to look at every
        # plane and not only the one being edited.
        with pytest.raises(MapEditError, match="ids must be unique"):
            edited(
                storeyed(),
                {
                    "op": "add_feature",
                    "level": 1,
                    "feature": {"id": "door-1", "kind": "spawn", "at": [1, 1]},
                },
            )

    def test_an_op_naming_a_level_the_map_lacks_is_refused(self) -> None:
        with pytest.raises(MapEditError, match="no level 4"):
            edited(storeyed(), {"op": "set_terrain", "rect": [1, 1, 1, 1],
                                "terrain": "wall", "level": 4})

    def test_a_level_must_be_named_by_a_whole_number(self) -> None:
        with pytest.raises(MapEditError, match="whole number"):
            edited(storeyed(), {"op": "set_terrain", "rect": [1, 1, 1, 1],
                                "terrain": "wall", "level": "up"})

    def test_a_document_wide_op_takes_no_level(self) -> None:
        with pytest.raises(MapEditError, match="unknown key"):
            edited(storeyed(), {"op": "set_name", "name": "keep", "level": 1})


class TestConnectorEdits:
    """``to_level``: the key that makes a storey walkable, now authorable.

    It was in the document's feature keys and in no edit operation's, so the
    only way to write a connector was to hand-edit the JSON — a gap that
    predates fixtures entirely.
    """

    def test_add_feature_authors_a_connector(self) -> None:
        doc = edited(
            storeyed(),
            {"op": "add_feature", "level": 1, "feature": {
                "id": "hatch", "kind": "stairs_down", "at": [1, 1], "to_level": 0,
            }},
        )
        assert doc.levels[1].features[-1].to_level == 0
        # And it is a connector to the fight, not merely a drawn glyph.
        assert to_grid(doc).levels[1].connectors[(1, 1)] == 0

    def test_a_connector_to_a_level_the_map_lacks_is_the_documents_refusal(self) -> None:
        # The refusals stay the document's, as every fixture key's do: the
        # service shapes the record and the final parse arbitrates it.
        with pytest.raises(MapError, match="there is no level 4 in this map"):
            edited(storeyed(), {"op": "add_feature", "feature": {
                "id": "hatch", "kind": "stairs_up", "at": [1, 1], "to_level": 4}})

    def test_a_connector_to_its_own_level_is_the_documents_refusal(self) -> None:
        with pytest.raises(MapError, match="leads to its own level"):
            edited(storeyed(), {"op": "add_feature", "feature": {
                "id": "hatch", "kind": "stairs_up", "at": [1, 1], "to_level": 0}})

    def test_a_connector_must_name_its_level_by_whole_number(self) -> None:
        with pytest.raises(MapError, match="whole number"):
            edited(storeyed(), {"op": "add_feature", "feature": {
                "id": "hatch", "kind": "stairs_up", "at": [1, 1], "to_level": "up"}})

    def test_set_feature_drops_a_connector_by_not_naming_it(self) -> None:
        # Replacement's other half: a key left out is a key removed, so the
        # stairway stops leading anywhere without a delete op existing.
        doc = edited(storeyed(), {"op": "set_feature", "feature": {
            "id": "stair-foot", "kind": "stairs_up", "at": [3, 3]}})
        assert doc.features[-1].to_level is None
        assert to_grid(doc).levels[0].connectors == {}


class TestLevelViews:
    def test_rendering_shows_the_level_it_is_asked_for(self) -> None:
        doc = storeyed()
        ground = service.render_ascii(doc)
        upper = service.render_ascii(doc, level=1)
        assert ground["rows"][2] == "#.%..#"
        assert upper["rows"][2] == "#....#"  # no difficult square on the gallery
        assert upper["level"] == 1

    def test_rendering_shows_the_levels_own_heights(self) -> None:
        upper = service.render_ascii(storeyed(), level=1, show_elevation=True)
        assert upper["elevation_legend"] == {"0": 10}  # the gallery's own datum
        ground = service.render_ascii(storeyed(), show_elevation=True)
        assert ground["elevation_legend"] == {"0": 0}

    def test_rendering_shows_the_levels_own_features(self) -> None:
        upper = service.render_ascii(storeyed(), level=1)
        assert [f["id"] for f in upper["features_in_view"]] == ["stair-head"]

    def test_rendering_lists_every_level_the_map_has(self) -> None:
        assert service.render_ascii(storeyed())["levels"] == [0, 1]

    def test_rendering_a_level_the_map_lacks_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no level 4"):
            service.render_ascii(storeyed(), level=4)

    def test_a_query_runs_on_the_level_it_is_asked_for(self) -> None:
        doc = storeyed()
        # (3, 4) is the ground's doorway and solid wall on the gallery, so the
        # same route is walkable downstairs and blocked upstairs.
        assert service.query(doc, "path", (1, 3), (3, 4), terrain=TERRAIN)["reachable"]
        above = service.query(doc, "path", (1, 3), (3, 4), terrain=TERRAIN, level=1)
        assert above["reachable"] is False

    def test_a_query_reports_the_levels_own_heights(self) -> None:
        result = service.query(storeyed(), "path", (1, 1), (2, 1), terrain=TERRAIN, level=1)
        assert (result["from_elevation"], result["to_elevation"]) == (10, 10)

    def test_a_query_on_a_level_the_map_lacks_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no level 4"):
            service.query(storeyed(), "distance", (0, 0), (1, 1), terrain=TERRAIN, level=4)
class TestPaletteEdits:
    def colored(self) -> MapDocument:
        """The room with its floor painted a color the renderers would not pick."""
        return edited(document(), {"op": "set_palette", "terrain": "floor", "color": "#d2440f"})

    def test_set_palette_colors_a_kind(self) -> None:
        doc = self.colored()
        assert doc.palette["floor"] == MapColor(light="#d2440f", dark="#d2440f")
        assert doc.provenance.edited is True

    def test_set_palette_takes_a_theme_pair(self) -> None:
        doc = edited(
            document(),
            {"op": "set_palette", "terrain": "water", "color": {"light": "#a9c6ce",
                                                                "dark": "#1f3a44"}},
        )
        assert doc.palette["water"] == MapColor(light="#a9c6ce", dark="#1f3a44")

    def test_a_shorthand_color_is_canonicalised(self) -> None:
        doc = edited(document(), {"op": "set_palette", "terrain": "floor", "color": "#ABC"})
        assert doc.palette["floor"].light == "#aabbcc"

    def test_a_null_color_clears_the_entry(self) -> None:
        doc = edited(self.colored(), {"op": "set_palette", "terrain": "floor", "color": None})
        assert dict(doc.palette) == {}
        assert "palette" not in json.loads(serialize(doc))

    def test_clearing_a_color_that_was_never_set_changes_nothing(self) -> None:
        before = document()
        after = edited(before, {"op": "set_palette", "terrain": "floor", "color": None})
        assert after is before

    def test_an_unrelated_edit_keeps_the_color_layer(self) -> None:
        # The trap: apply_edits rebuilds the whole payload, so a layer the edit
        # state forgets is one every unrelated edit silently discards.
        doc = edited(self.colored(), {"op": "set_name", "name": "renamed"})
        assert doc.name == "renamed"
        assert doc.palette["floor"].light == "#d2440f"

    def test_a_resize_keeps_the_color_layer(self) -> None:
        # Colors have no coordinate frame, so the op that reframes everything
        # else must leave them exactly where they were.
        doc = edited(self.colored(), {"op": "resize", "width": 8, "height": 5})
        assert doc.palette["floor"].light == "#d2440f"

    def test_a_kind_with_no_glyph_may_still_be_colored(self) -> None:
        doc = edited(document(), {"op": "set_palette", "terrain": "water", "color": "#a9c6ce"})
        assert "water" not in doc.legend.values()
        assert doc.palette["water"].light == "#a9c6ce"

    def test_an_unknown_terrain_kind_is_refused(self) -> None:
        with pytest.raises(MapEditError, match="not defined by the active content"):
            edited(document(), {"op": "set_palette", "terrain": "lava", "color": "#d2440f"})

    def test_a_named_css_color_is_refused(self) -> None:
        with pytest.raises(MapEditError, match="hex color"):
            edited(document(), {"op": "set_palette", "terrain": "floor", "color": "red"})

    def test_a_url_color_is_refused(self) -> None:
        with pytest.raises(MapEditError, match="hex color"):
            edited(
                document(),
                {"op": "set_palette", "terrain": "floor", "color": "url(http://x.invalid/i.png)"},
            )

    def test_a_half_finished_pair_is_refused(self) -> None:
        with pytest.raises(MapEditError, match='both "light" and "dark"'):
            edited(
                document(),
                {"op": "set_palette", "terrain": "floor", "color": {"light": "#aabbcc"}},
            )


def sluice_payload() -> dict[str, Any]:
    """Two rooms either side of a sluice gate that floods the far one.

    The gate stands in the only gap in the dividing wall, so a shut gate leaves
    the bottom corridor as the way round; its overlay says what the far room is
    in each state — dry floor while it is shut, water five feet lower once it is
    open. Nothing but the gate's own record says any of that, which is what
    makes it the fixture case: the tiles are floor either way.
    """
    return {
        **payload(),
        "name": "sluice",
        "grid": {"width": 7, "height": 6, "cell_feet": 5},
        "tiles": [
            "#######",
            "#..#..#",
            "#..#..#",
            "#..#..#",
            "#.....#",
            "#######",
        ],
        "features": [
            {
                "id": "sluice gate",
                "kind": "door",
                "at": [3, 2],
                "orientation": "vertical",
                "state": "closed",
                "affects": [
                    {
                        "cells": [[4, 1], [5, 1], [4, 2], [5, 2], [4, 3], [5, 3]],
                        "terrain": {"closed": "floor", "open": "water"},
                        "elevation": {"closed": 0, "open": -5},
                    }
                ],
            }
        ],
    }


#: The far room, in the order the document writes it: row, then column.
FLOODED = ((4, 1), (5, 1), (4, 2), (5, 2), (4, 3), (5, 3))


def sluiced(state: str = "closed") -> MapDocument:
    """The sluice map with its gate authored in ``state``."""
    raw = sluice_payload()
    raw["features"][0]["state"] = state
    return parse_document(raw, source="sluice", terrain=TERRAIN)


def spiked() -> MapDocument:
    """The sluice, pinned by two spikes — one already pulled, one still driven."""
    raw = sluice_payload()
    raw["features"][0]["requires"] = ["north spike", "south spike"]
    raw["features"] += [
        {
            "id": "north spike", "kind": "spike", "at": [1, 1], "state": "closed",
            "costs_action": True, "check": {"ability": "strength", "dc": 15},
        },
        {
            "id": "south spike", "kind": "spike", "at": [1, 3], "state": "open",
            "costs_action": True, "check": {"ability": "strength", "dc": 15},
        },
    ]
    return parse_document(raw, source="spiked", terrain=TERRAIN)


class TestFixtureEdits:
    def gated(self, *affects: dict[str, Any]) -> MapDocument:
        """The base room with a second door governing the caller's overlays."""
        return edited(
            document(),
            {"op": "add_feature", "feature": {
                "id": "sluice", "kind": "door", "at": [3, 3],
                "orientation": "vertical", "state": "closed",
                "affects": list(affects),
            }},
        )

    def test_every_fixture_key_rides_through_to_the_document(self) -> None:
        # The service checks the shape of an overlay and nothing else: the six
        # keys are the document's to arbitrate, which is why one edit can add a
        # fixture the format understands without the service knowing the rules.
        feature: dict[str, Any] = {
            "id": "lever", "kind": "lever", "at": [1, 3], "state": "closed",
            "terrain": {"closed": "wall", "open": "floor"},
            "elevation": {"closed": 0, "open": 5},
            "affects": [
                {"cells": [[4, 1], [4, 2]],
                 "terrain": {"closed": "floor", "open": "water"}},
            ],
            "requires": ["door-1"],
            "costs_action": True,
            "check": {"ability": "strength", "dc": 15},
        }
        doc = edited(document(), {"op": "add_feature", "feature": feature})
        assert json.loads(serialize(doc))["features"][-1] == feature

    def test_an_overlay_rect_and_its_cells_write_the_same_document(self) -> None:
        # The rect is the author's shorthand — one line instead of forty pairs —
        # and it is expanded before the payload, so the file has one shape and a
        # resize has one thing to translate.
        flood = {"terrain": {"closed": "floor", "open": "water"}}
        by_rect = self.gated({"rect": [1, 1, 2, 2], **flood})
        by_cells = self.gated({"cells": [[2, 2], [1, 1], [2, 1], [1, 2]], **flood})
        assert serialize(by_rect) == serialize(by_cells)
        assert by_rect.features[-1].affects[0].cells == ((1, 1), (2, 1), (1, 2), (2, 2))

    def test_an_overlay_naming_both_a_rect_and_cells_is_refused(self) -> None:
        with pytest.raises(MapEditError, match="entry #0 needs exactly one of 'rect'"):
            self.gated({"rect": [1, 1, 2, 2], "cells": [[1, 1]],
                        "terrain": {"closed": "floor", "open": "water"}})

    def test_an_overlay_naming_neither_a_rect_nor_cells_is_refused(self) -> None:
        with pytest.raises(MapEditError, match="entry #0 needs exactly one of 'rect'"):
            self.gated({"terrain": {"closed": "floor", "open": "water"}})

    def test_an_overlay_rect_off_the_map_is_refused(self) -> None:
        with pytest.raises(MapEditError, match="reaches outside"):
            self.gated({"rect": [4, 4, 9, 9],
                        "terrain": {"closed": "floor", "open": "water"}})

    def test_an_overlay_cell_off_the_map_is_refused(self) -> None:
        with pytest.raises(MapEditError, match="each cell is .9, 9., outside"):
            self.gated({"cells": [[9, 9]],
                        "terrain": {"closed": "floor", "open": "water"}})

    def test_affects_must_be_a_list_of_overlay_objects(self) -> None:
        with pytest.raises(MapEditError, match="'affects' must be a list"):
            edited(document(), {"op": "add_feature", "feature": {
                "id": "sluice", "kind": "door", "at": [3, 3],
                "orientation": "vertical", "state": "closed",
                "affects": {"cells": [[1, 1]]},
            }})
        with pytest.raises(MapEditError, match="'affects' entry #0 must be an object"):
            edited(document(), {"op": "add_feature", "feature": {
                "id": "sluice", "kind": "door", "at": [3, 3],
                "orientation": "vertical", "state": "closed",
                "affects": ["the whole east room"],
            }})

    def test_a_square_another_fixture_governs_is_refused_by_the_document(self) -> None:
        # Not the service's rule to enforce, and deliberately so: the claim is
        # the format's, and the final parse is where it is answered.
        with pytest.raises(MapError, match="'sluice gate' already governs"):
            edited(sluiced(), {"op": "add_feature", "feature": {
                "id": "wheel", "kind": "wheel", "at": [1, 4], "state": "closed",
                "affects": [{"cells": [[4, 2]],
                             "terrain": {"closed": "floor", "open": "difficult"}}],
            }})

    def test_an_unrelated_edit_keeps_every_fixture_key(self) -> None:
        # The trap: apply_edits rebuilds the whole payload from the edit state,
        # so a layer that state does not hold is one every unrelated edit
        # silently discards. Nested keys are no different from top-level ones.
        before = spiked()
        after = edited(before, {"op": "paint", "cells": [[1, 4]], "terrain": "difficult"})
        assert after.tiles[4] == "#%....#"
        assert json.dumps(json.loads(serialize(after))["features"]) == json.dumps(
            json.loads(serialize(before))["features"]
        )

    def test_a_resize_moves_overlay_cells_with_the_anchor(self) -> None:
        # A layer nested inside a record is still a layer: only 'at' used to
        # move, so a fixture's overlay cells stayed where they were and
        # mislocated the flood by exactly the anchor offset — visible only on a
        # map somebody had resized.
        shifts = {
            "top-left": (0, 0), "top-right": (4, 0),
            "bottom-left": (0, 4), "bottom-right": (4, 4),
        }
        moved: dict[str, tuple[Square, tuple[Square, ...]]] = {}
        for anchor in shifts:
            doc = edited(
                sluiced(), {"op": "resize", "width": 11, "height": 10, "anchor": anchor}
            )
            gate = doc.features[0]
            moved[anchor] = (gate.at, gate.affects[0].cells)
        assert moved == {
            anchor: (
                (3 + dx, 2 + dy),
                tuple((x + dx, y + dy) for x, y in FLOODED),
            )
            for anchor, (dx, dy) in shifts.items()
        }

    def test_a_resize_crops_overlay_cells_to_the_new_frame(self) -> None:
        doc = edited(sluiced(), {"op": "resize", "width": 5, "height": 6})
        gate = doc.features[0]
        assert gate.at == (3, 2)
        # The far room's east column fell off with the squares it described.
        assert gate.affects[0].cells == ((4, 1), (4, 2), (4, 3))

    def test_a_resize_that_empties_a_group_drops_it_and_keeps_the_fixture(self) -> None:
        doc = edited(sluiced(), {"op": "resize", "width": 4, "height": 6})
        gate = doc.features[0]
        assert gate.at == (3, 2)
        assert gate.affects == ()
        assert "affects" not in json.loads(serialize(doc))["features"][0]

    def test_a_resize_refuses_to_drop_a_fixture_another_one_requires(self) -> None:
        # Left to happen, the final re-parse refuses the whole edit naming a
        # missing prerequisite rather than the resize that removed it. The
        # editor's own resize refuses this case, and the two are meant to agree.
        with pytest.raises(MapEditError, match="would push 'north spike' off the map"):
            edited(
                spiked(),
                {"op": "resize", "width": 5, "height": 6, "anchor": "top-right"},
            )

    def test_a_prerequisite_is_protected_across_a_storey(self) -> None:
        # A prerequisite is not a reach: which floor the thing a fixture waits
        # on stands on is the fiction's business, so the check spans the
        # document rather than the plane being edited.
        raw = sluice_payload()
        raw["features"][0]["requires"] = ["hatch"]
        raw["levels"] = [
            {
                "index": 1, "name": "gallery", "tiles": raw["tiles"],
                "features": [
                    {"id": "hatch", "kind": "hatch", "at": [5, 4], "state": "closed"}
                ],
            }
        ]
        with pytest.raises(MapEditError, match="would push 'hatch' off the map"):
            edited(
                parse_document(raw, source="crossed", terrain=TERRAIN),
                {"op": "resize", "width": 5, "height": 6},
            )

    def test_dropping_the_fixture_that_does_the_requiring_is_allowed(self) -> None:
        # Only a *surviving* fixture's prerequisites are protected. This crop
        # takes the gate and the spike it waits on together, which leaves
        # nothing asking for what is gone — refusing it would refuse cropping a
        # map down to a room the gate is not in.
        doc = edited(spiked(), {"op": "resize", "width": 7, "height": 2})
        assert [f.id for f in doc.features] == ["north spike"]

    def test_a_refused_resize_leaves_the_document_byte_identical(self) -> None:
        before = spiked()
        frozen = serialize(before)
        with pytest.raises(MapEditError) as caught:
            service.apply_edits(
                before,
                [
                    {"op": "set_name", "name": "halfway"},
                    {"op": "resize", "width": 5, "height": 6, "anchor": "top-right"},
                ],
                terrain=TERRAIN,
            )
        assert caught.value.op_index == 1
        assert "operation #1" in str(caught.value)
        assert serialize(before) == frozen  # the input document is untouched


class TestSetFeature:
    """``set_feature``: edit one feature in place, by id.

    Before it, everything but a door's state was ``remove_feature`` plus
    ``add_feature`` — which reorders the features array and, because
    ``add_feature`` takes a ``level``, can silently move a fixture to another
    storey.
    """

    def test_a_feature_is_edited_in_place_and_keeps_its_position(self) -> None:
        doc = edited(
            document(),
            {"op": "set_feature", "feature": {
                "id": "door-1", "kind": "door", "at": [3, 4],
                "orientation": "vertical", "state": "open",
            }},
        )
        # remove_feature + add_feature would have written it last instead.
        assert [f.id for f in doc.features] == ["door-1", "spawn-party"]
        assert doc.features[0].orientation == "vertical"
        assert doc.features[0].state == "open"

    def test_the_record_is_replaced_rather_than_merged_into(self) -> None:
        # The decision this op is written around: what the call names is what
        # the document holds. A merge would make the result depend on state the
        # call never mentions, and would leave no way to clear a key at all.
        doc = edited(
            sluiced(),
            {"op": "set_feature", "feature": {
                "id": "sluice gate", "kind": "door", "at": [3, 2],
                "orientation": "vertical", "state": "closed",
            }},
        )
        assert doc.features[0].affects == ()
        assert "affects" not in json.loads(serialize(doc))["features"][0]

    def test_a_call_shaped_like_a_merge_is_refused_saying_it_replaces(self) -> None:
        # The one-key patch a caller who assumed merge would write. Every such
        # call omits 'kind' or 'at', so every such call is refused — and the
        # refusal is where the semantics are stated, because a replace that
        # honoured it silently would drop the rest of the record.
        with pytest.raises(MapEditError, match="a key left out is a key removed"):
            edited(document(), {"op": "set_feature",
                                "feature": {"id": "door-1", "state": "open"}})
        with pytest.raises(MapEditError, match="'at' is required.*key removed"):
            edited(document(), {"op": "set_feature",
                                "feature": {"id": "door-1", "kind": "door"}})
        with pytest.raises(MapEditError, match="a door needs 'orientation'.*key removed"):
            edited(document(), {"op": "set_feature",
                                "feature": {"id": "door-1", "kind": "door", "at": [3, 4]}})

    def test_the_refusal_points_at_toggle_door_for_a_doors_state(self) -> None:
        # toggle_door stays for exactly the case a merge would have served.
        # Matched on the whole clause: every refusal in this module lists the
        # valid ops, so a bare "toggle_door" would pass against any of them.
        with pytest.raises(MapEditError, match="toggle_door, which flips a door's state"):
            edited(document(), {"op": "set_feature",
                                "feature": {"id": "door-1", "state": "open"}})

    def test_setting_a_feature_that_does_not_exist_lists_what_does(self) -> None:
        with pytest.raises(
            MapEditError, match="no feature named 'portcullis'.*door-1, spawn-party"
        ):
            edited(document(), {"op": "set_feature", "feature": {
                "id": "portcullis", "kind": "door", "at": [3, 4],
                "orientation": "vertical", "state": "open"}})

    def test_an_unknown_feature_key_names_the_valid_ones(self) -> None:
        with pytest.raises(MapEditError, match=r"unknown key\(s\): 'latch'.*to_level"):
            edited(document(), {"op": "set_feature", "feature": {
                "id": "door-1", "kind": "door", "at": [3, 4],
                "orientation": "vertical", "state": "open", "latch": "left"}})

    def test_set_feature_takes_no_level_so_it_cannot_move_a_storey(self) -> None:
        # The relocation hazard the re-add pair carries: add_feature takes a
        # level, so re-adding a fixture can silently rehouse it one floor up.
        with pytest.raises(MapEditError, match=r"unknown key\(s\): 'level'"):
            edited(storeyed(), {"op": "set_feature", "level": 1, "feature": {
                "id": "stair-foot", "kind": "stairs_up", "at": [3, 3], "to_level": 1}})

    def test_a_feature_on_a_storey_is_found_by_id_and_stays_there(self) -> None:
        doc = edited(
            storeyed(),
            {"op": "set_feature", "feature": {
                "id": "stair-head", "kind": "stairs_down", "at": [2, 3], "to_level": 0,
            }},
        )
        assert [f.id for f in doc.levels[1].features] == ["stair-head"]
        assert doc.levels[1].features[0].at == (2, 3)
        assert [f.id for f in doc.levels[0].features] == [
            "door-1", "spawn-party", "stair-foot",
        ]

    def test_every_field_a_feature_can_carry_round_trips_through_a_set(self) -> None:
        feature: dict[str, Any] = {
            "id": "north spike", "kind": "lever", "at": [2, 3],
            "orientation": "vertical", "state": "open", "team": "party",
            "terrain": {"closed": "wall", "open": "floor"},
            "elevation": {"closed": 0, "open": 5},
            "affects": [
                {"cells": [[2, 1], [2, 2]],
                 "terrain": {"closed": "floor", "open": "water"}},
            ],
            "requires": ["south spike"],
            "costs_action": True,
            "check": {"ability": "strength", "dc": 15},
        }
        doc = edited(spiked(), {"op": "set_feature", "feature": feature})
        # Index 1 is where the spike stood, and where it stays.
        assert json.loads(serialize(doc))["features"][1] == feature

    def test_an_overlay_may_be_given_as_a_rect_here_too(self) -> None:
        by_rect = edited(sluiced(), {"op": "set_feature", "feature": {
            "id": "sluice gate", "kind": "door", "at": [3, 2],
            "orientation": "vertical", "state": "closed",
            "affects": [{"rect": [4, 1, 2, 3],
                         "terrain": {"closed": "floor", "open": "water"}}]}})
        assert by_rect.features[0].affects[0].cells == FLOODED

    def test_the_document_arbitrates_the_result_of_a_set(self) -> None:
        # The pattern add_feature already follows: the service shapes the
        # record and the final parse_document decides. Which fixture governs a
        # square is the format's rule, not this operation's.
        with pytest.raises(MapError, match="'sluice gate' already governs"):
            edited(spiked(), {"op": "set_feature", "feature": {
                "id": "north spike", "kind": "spike", "at": [1, 1], "state": "closed",
                "affects": [{"cells": [[4, 2]],
                             "terrain": {"closed": "floor", "open": "difficult"}}]}})

    def test_a_refused_set_feature_leaves_the_document_byte_identical(self) -> None:
        before = spiked()
        frozen = serialize(before)
        with pytest.raises(MapEditError) as caught:
            service.apply_edits(
                before,
                [
                    {"op": "set_name", "name": "halfway"},
                    {"op": "set_feature", "feature": {"id": "north spike", "kind": "spike"}},
                ],
                terrain=TERRAIN,
            )
        assert caught.value.op_index == 1
        assert "operation #1" in str(caught.value)
        assert serialize(before) == frozen  # the input document is untouched


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
        with pytest.raises(MapEditError, match="unknown key.*'color'"):
            edited(document(), {"op": "set_name", "name": "x", "color": "red"})

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

    def test_a_fixture_shows_its_state_whatever_kind_it_is(self) -> None:
        # Carrying a state is what makes a feature a fixture, so a spike draws
        # as one — invisible before, for not being a door. No sixth reserved
        # glyph: the set is closed, a legend claiming one is a validation error,
        # and adding to it would invalidate documents that legend it today.
        rendered = service.render_ascii(spiked())
        assert rendered["rows"][1] == "#+.#..#"  # the spike still driven in
        assert rendered["rows"][3] == "#/.#..#"  # the one already pulled
        assert rendered["rows"][2] == "#..+..#"  # the gate the two of them pin

    def test_a_drawn_annotation_keeps_its_glyph_even_when_it_can_be_operated(
        self,
    ) -> None:
        # An operable hatch is still a stairway. The glyph says what a thing
        # *is*, so drawing it '+' would read as a door in the corridor; its
        # state is not lost, because features_in_view carries the whole record.
        hatch = edited(
            document(),
            {"op": "add_feature", "feature": {
                "id": "hatch", "kind": "stairs_down", "at": [2, 2],
                "state": "closed"}},
        )
        rendered = service.render_ascii(hatch)
        assert rendered["rows"][2][2] == ">"
        assert [f["state"] for f in rendered["features_in_view"]
                if f["id"] == "hatch"] == ["closed"]

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


class TestRenderFixtureStates:
    """``render_ascii(open=…)``: the map a fight is on, not the map as authored.

    Every assertion here is about one document rendered twice, because the whole
    point is that nothing in the file changes: the sluice map's tiles are floor
    on both sides of the gate, and only the gate's own record says the far room
    floods. A render that reads tiles alone cannot tell the two apart.
    """

    def legended(self, state: str = "closed") -> MapDocument:
        """The sluice map, drawing water with a glyph of the document's choosing.

        Deliberately not ``~``: that is the glyph the format's default legend
        gives water, so a render that reached for it anyway would still pass a
        document that spends its own.
        """
        raw = sluice_payload()
        raw["features"][0]["state"] = state
        raw["legend"] = {**raw["legend"], "w": "water"}
        return parse_document(raw, source="sluice", terrain=TERRAIN)

    def trapdoor(self, state: str = "closed") -> MapDocument:
        """A fixture whose only overlay is the ground dropping out from under it."""
        raw = sluice_payload()
        raw["features"][0] = {
            "id": "trapdoor", "kind": "hatch", "at": [3, 2], "state": state,
            "affects": [
                {"cells": [[4, 2], [5, 2]], "elevation": {"closed": 0, "open": -10}}
            ],
        }
        return parse_document(raw, source="trapdoor", terrain=TERRAIN)

    def test_a_render_naming_no_fixture_is_the_map_exactly_as_authored(self) -> None:
        # The pin on the default: every channel below resolves through fixture
        # state, and a caller that names none must see the render it saw before,
        # glyph for glyph and legend entry for legend entry.
        rendered = service.render_ascii(sluiced(), show_elevation=True)
        assert rendered["rows"] == [
            "#######", "#..#..#", "#..+..#", "#..#..#", "#.....#", "#######",
        ]
        assert rendered["legend"] == {"#": "wall", ".": "floor"}
        assert rendered["elevation_rows"] == ["0000000"] * 6
        assert rendered["elevation_legend"] == {"0": 0}
        assert [entry["id"] for entry in rendered["features_in_view"]] == ["sluice gate"]

    def test_naming_a_fixture_open_floods_the_room_and_flips_its_glyph(self) -> None:
        rendered = service.render_ascii(self.legended(), open=["sluice gate"])
        assert rendered["rows"] == [
            "#######", "#..#ww#", "#../ww#", "#..#ww#", "#.....#", "#######",
        ]
        assert rendered["legend"]["w"] == "water"

    def test_an_empty_set_shuts_a_fixture_the_document_authored_open(self) -> None:
        # `open=[]` is a set, not an absence: it says every fixture is shut. A
        # truthiness test here would read it as "nothing given" and hand back the
        # authored open gate, which is the state the fight has just left.
        rendered = service.render_ascii(self.legended("open"), open=[])
        assert rendered["rows"][1] == "#..#..#"
        assert rendered["rows"][2] == "#..+..#"

    def test_naming_either_linked_door_opens_both_leaves(self) -> None:
        left = service.render_ascii(linked_document(), open=["door-2"])
        right = service.render_ascii(linked_document(), open=["door-1"])
        shut = service.render_ascii(linked_document(), open=[])

        assert left["rows"][4] == "##//##"
        assert right["rows"][4] == "##//##"
        assert shut["rows"][4] == "##++##"

    def test_a_fixtures_own_square_resolves_through_the_state_it_is_given(self) -> None:
        # Under show_features the reserved glyph covers the gate's square either
        # way, so the terrain layer is only visible with the overlay off — and it
        # has to move too, or a downsampled block votes on the authored tile.
        shut = service.render_ascii(self.legended(), open=[], show_features=False)
        ajar = service.render_ascii(self.legended(), open=["sluice gate"],
                                    show_features=False)
        assert shut["rows"][2] == "#..=..#"
        assert ajar["rows"][2] == "#..=ww#"
        assert shut["legend"]["="] == "door-closed"
        assert ajar["legend"]["="] == "door-open"

    def test_an_overlay_that_only_moves_the_ground_moves_only_the_contour(
        self,
    ) -> None:
        shut = service.render_ascii(self.trapdoor(), open=[], show_elevation=True)
        dropped = service.render_ascii(
            self.trapdoor(), open=["trapdoor"], show_elevation=True
        )
        assert dropped["rows"] == shut["rows"][:2] + ["#../..#"] + shut["rows"][3:]
        assert shut["elevation_rows"] == ["0000000"] * 6
        assert dropped["elevation_rows"] == [
            "1111111", "1111111", "1111001", "1111111", "1111111", "1111111",
        ]
        assert dropped["elevation_legend"] == {"0": -10, "1": 0}

    def test_a_kind_the_document_legends_keeps_the_documents_own_glyph(self) -> None:
        # The first tier, and the one that keeps a render readable: this document
        # draws water as 'w', so the flood does too rather than borrowing.
        rendered = service.render_ascii(self.legended(), open=["sluice gate"])
        assert "~" not in "".join(rendered["rows"])
        assert rendered["legend"]["w"] == "water"

    def test_a_kind_with_no_glyph_borrows_the_formats_before_a_spare(self) -> None:
        # sluiced() legends floor, wall and difficult and nothing else, so both
        # of the kinds its gate introduces need a glyph. Water has one in the
        # format's default legend and this document leaves it free, so the flood
        # reads as it does on every generated map; door-open has none anywhere
        # and falls to the first spare.
        rendered = service.render_ascii(sluiced(), open=["sluice gate"])
        assert rendered["rows"][1] == "#..#~~#"
        assert rendered["legend"]["~"] == "water"
        assert rendered["legend"][service.SPARE_GLYPHS[0]] == "door-open"

    def test_a_borrowed_glyph_is_never_reserved_and_never_the_documents(self) -> None:
        rendered = service.render_ascii(sluiced(), open=["sluice gate"])
        borrowed = set(rendered["legend"]) - set(sluiced().legend)
        assert borrowed == {"~", service.SPARE_GLYPHS[0]}
        assert not (borrowed & RESERVED_GLYPHS)

    def test_a_map_with_no_character_left_to_borrow_is_refused(self) -> None:
        raw = sluice_payload()
        crowded = {**raw["legend"], "~": "floor"}
        crowded.update({glyph: "floor" for glyph in service.SPARE_GLYPHS})
        raw["legend"] = crowded
        with pytest.raises(ValueError, match="no free glyph left.*set_legend"):
            service.render_ascii(
                parse_document(raw, source="crowded", terrain=TERRAIN),
                open=["sluice gate"],
            )

    def test_a_name_no_fixture_here_answers_to_is_ignored(self) -> None:
        # `open` is a fight's whole set and fixture names are unique map-wide, so
        # a ground-floor render is routinely handed names belonging upstairs.
        # Refusing them would make an ordinary multi-storey fight unrenderable.
        rendered = service.render_ascii(sluiced(), open=["hatch on the gallery"])
        assert rendered["rows"][1] == "#..#..#"
        assert rendered["rows"][2] == "#..+..#"

    def test_the_flood_survives_downsampling_as_a_majority(self) -> None:
        # Proof the terrain layer moved rather than one cell being overdrawn:
        # the third block of the middle row holds no feature glyph at all and
        # still votes itself water, four squares to nothing.
        rendered = service.render_ascii(
            self.legended(), open=["sluice gate"], downsample=2
        )
        assert rendered["rows"] == ["####", "#/w#", "####"]
        assert service.render_ascii(
            self.legended(), open=[], downsample=2
        )["rows"] == ["####", "#+.#", "####"]


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

    def test_a_shut_fixture_leaves_the_room_it_governs_dry(self) -> None:
        # "Can we reach the far room once the sluice is open?" is a planning
        # question, so the composers here resolve a fixture's claims exactly as
        # the encounter's do — from the state the document authored, since there
        # is no fight to have moved one.
        result = service.query(sluiced(), "path", (1, 2), (5, 2), terrain=TERRAIN)
        assert result["reachable"] is True
        assert result["cost_feet"] == 20  # four dry squares at 5 feet apiece
        assert [3, 2] not in result["squares"]  # round the shut gate, not through it
        assert (result["from_elevation"], result["to_elevation"]) == (0, 0)

    def test_the_same_room_authored_open_is_flooded_and_costs_double(self) -> None:
        result = service.query(sluiced("open"), "path", (1, 2), (5, 2), terrain=TERRAIN)
        assert result["reachable"] is True
        # Nothing but the gate's overlay says the room is water: the tiles under
        # it are the same floor either way, so 30 feet is the flood being seen.
        assert result["cost_feet"] == 30
        assert (result["from_elevation"], result["to_elevation"]) == (0, -5)

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
        assert service.maps_root({"FIVEE_SIM_PROJECT_DIR": "/neutral"}) == Path(
            "/neutral/.fivee-sim/maps"
        )
        assert service.maps_root({"CLAUDE_PROJECT_DIR": "/repo"}) == Path(
            "/repo/.fivee-sim/maps"
        )
        assert service.maps_root({
            "FIVEE_SIM_PROJECT_DIR": "/neutral",
            "CLAUDE_PROJECT_DIR": "/claude",
        }) == Path("/neutral/.fivee-sim/maps")
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

    def test_a_symlink_out_of_a_listed_directory_is_not_catalogued(
        self, tmp_path: Path
    ) -> None:
        # The listing applies the content loader's containment rule, but unlike
        # the loader it reports nothing — a refused file is simply absent. That
        # silence is deliberate (a listing shows what is usable) and it is also
        # why the rule needs pinning here: with no diagnostic to assert on,
        # absence from the catalogue is the only evidence the check still runs.
        outside = tmp_path / "outside"
        outside.mkdir()
        service.save_file(document(), outside / "secret.json")
        maps = tmp_path / "maps"
        maps.mkdir()
        try:
            (maps / "escape.json").symlink_to(outside / "secret.json")
        except OSError:  # pragma: no cover - platform without symlinks
            pytest.skip("symlinks unavailable")
        service.save_file(document(), maps / "own.json")

        listed = service.list_maps([maps])
        assert [Path(entry["path"]).name for entry in listed] == ["own.json"]
