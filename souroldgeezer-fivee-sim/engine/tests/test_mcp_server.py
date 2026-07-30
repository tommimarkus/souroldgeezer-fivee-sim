"""Adapter tests for the MCP tool surface.

These exercise the tools as callables. The protocol handshake itself is checked
separately by ``scripts/check-mcp-handshake.py``, which speaks real JSON-RPC over
stdio; here the concern is input validation, seed reporting, and that state moves
through the session correctly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.maps import parse_document
from fivee_sim.mcp_server import server as api
from fivee_sim.service import maps as map_service

GOBLIN: dict[str, Any] = {
    "monster": "Goblin Warrior",
    "label": "Goblin",
    "team": "monsters",
    "position": 5,
}
HERO: dict[str, Any] = {
    "name": "Thora",
    "team": "party",
    "ac": 16,
    "max_hp": 30,
    "position": 0,
    "abilities": {"strength": 16, "dexterity": 14, "constitution": 14},
    "attacks": [
        {
            "name": "Longsword",
            "attack_bonus": 5,
            "damage": "1d8+3",
            "damage_type": "slashing",
            "kind": "melee",
        }
    ],
}


class TestPrimitives:
    def test_a_seed_is_always_reported_so_any_roll_can_be_replayed(self) -> None:
        without = api.roll("2d6+3")
        assert isinstance(without["seed"], int)
        replay = api.roll("2d6+3", seed=int(without["seed"]))
        assert replay["total"] == without["total"]

    def test_advantage_applies_to_a_lone_d20(self) -> None:
        result = api.roll("d20", advantage="advantage", seed=5)
        assert len(result["rolls"]) == 2
        assert result["natural"] == max(result["rolls"])

    def test_advantage_is_ignored_for_other_expressions(self) -> None:
        result = api.roll("2d6", advantage="advantage", seed=5)
        assert result["advantage"] == "none"
        assert len(result["rolls"]) == 2

    def test_a_bad_advantage_value_lists_what_is_allowed(self) -> None:
        with pytest.raises(api.ToolError, match="advantage must be one of"):
            api.roll("d20", advantage="huge")

    def test_a_bad_dice_expression_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="not a dice expression"):
            api.roll("two sixes")

    def test_check_and_save_report_success_against_the_dc(self) -> None:
        assert api.check(modifier=20, dc=5, seed=1)["success"]
        assert not api.check(modifier=-20, dc=25, seed=1)["success"]
        assert api.save(modifier=20, dc=5, seed=1)["success"]

    def test_an_auto_failed_save_fails_despite_a_huge_modifier(self) -> None:
        result = api.save(modifier=50, dc=10, auto_fail=True, seed=1)
        assert result["auto_failed"]
        assert not result["success"]


class TestLookup:
    def test_no_topic_lists_everything_loaded(self) -> None:
        listing = api.lookup_rule()
        assert "prone" in listing["conditions"]
        assert "Fireball" in listing["spells"]
        assert "Goblin Warrior" in listing["creatures"]
        # The listing has to say what it is a listing *of*: with packs loaded or the
        # bundled slice excluded, "what is available" is not a fixed answer.
        assert listing["builtin"] == "include"
        assert "SRD 5.2" in listing["provenance"]

    def test_a_condition_reports_only_its_active_effects(self) -> None:
        result = api.lookup_rule("restrained")
        assert result["kind"] == "condition"
        assert result["effects"]["speed_zero"] is True

    def test_a_condition_without_combat_effects_says_so(self) -> None:
        assert "note" in api.lookup_rule("deafened")["effects"]

    def test_a_spell_reports_its_resolution_values(self) -> None:
        result = api.lookup_rule("fireball")
        assert result["kind"] == "spell"
        assert result["damage"] == "8d6"
        assert result["save"] == "dexterity"
        assert result["radius"] == 20

    def test_a_creature_returns_its_record_including_unmodelled_traits(self) -> None:
        result = api.lookup_rule("zombie")
        assert result["kind"] == "creature"
        assert result["ac"] == 8
        # Undead Fortitude is modelled now, so it rides the record as a flag; the
        # unmodelled list keeps what the engine still skips.
        assert result["undead_fortitude"] is True
        assert any("Exhaustion" in note for note in result["unmodelled"])

    def test_every_entry_names_the_pack_it_came_from(self) -> None:
        # Provenance has to survive the merge: once SRD and original material can sit
        # in one session, "where did this come from?" must be answerable per entry.
        for topic in ("prone", "Fireball", "zombie"):
            entry = api.lookup_rule(topic)
            assert entry["source"].startswith("bundled:"), entry["source"]
            assert entry["provenance"] == "SRD 5.2"
            assert "unmodelled" in entry, "the skill tells Claude to check this field"

    def test_a_terrain_kind_resolves_and_is_listed(self) -> None:
        listing = api.lookup_rule()
        assert "difficult" in listing["terrain"]
        entry = api.lookup_rule("difficult")
        assert entry["kind"] == "terrain"
        assert entry["effects"]["move_cost_multiplier"] == 2
        assert entry["source"] == "bundled:terrain"

    def test_a_miss_points_at_what_is_actually_loaded(self) -> None:
        # Not "only SRD content ships" any more — that stopped being true the moment
        # a campaign could load its own. The miss has to send the caller to the
        # listing rather than assert a fixed catalogue.
        with pytest.raises(api.ToolError, match="content_status"):
            api.lookup_rule("Beholder")


class TestEncounterFlow:
    def test_create_act_and_advance_move_the_fight_along(self) -> None:
        created = api.encounter_create([HERO, GOBLIN], seed=11)
        encounter_id = str(created["encounter_id"])
        assert created["seed"] == 11
        assert created["state"]["round"] == 1

        # Walk to whoever's turn it is, then attack the other side.
        state = api.encounter_state(encounter_id)
        actor = str(state["turn"])
        opponent = "Goblin" if actor == "Thora" else "Thora"
        attack = "Longsword" if actor == "Thora" else "Scimitar"
        acted = api.encounter_act(
            encounter_id, kind="attack", target=opponent, attack=attack
        )
        assert acted["events"]
        assert acted["state"]["turn"] == actor

        advanced = api.encounter_advance(encounter_id)
        assert advanced["state"]["turn"] != actor or advanced["state"]["over"]

    def test_state_is_the_authoritative_view(self) -> None:
        created = api.encounter_create([HERO, GOBLIN], seed=3)
        state = api.encounter_state(str(created["encounter_id"]))
        names = {entry["name"] for entry in state["combatants"]}
        assert names == {"Thora", "Goblin"}
        assert all("hp" in entry for entry in state["combatants"])

    def test_an_unknown_encounter_id_lists_the_active_ones(self) -> None:
        api.encounter_create([HERO, GOBLIN], seed=1)
        with pytest.raises(api.ToolError, match="active:"):
            api.encounter_state("enc-does-not-exist")

    def test_an_unknown_action_kind_lists_the_allowed_ones(self) -> None:
        created = api.encounter_create([HERO, GOBLIN], seed=1)
        with pytest.raises(api.ToolError, match="kind must be one of"):
            api.encounter_act(str(created["encounter_id"]), kind="parry")

    def test_an_illegal_action_is_refused_with_the_reason(self) -> None:
        created = api.encounter_create([HERO, GOBLIN], seed=1)
        encounter_id = str(created["encounter_id"])
        with pytest.raises(api.ToolError, match="no combatant named"):
            api.encounter_act(encounter_id, kind="attack", target="Nobody")


class TestPlanarPositions:
    """The two-dimensional wire format: [x, y] in state, accepted on input."""

    def advance_to_thora(self, encounter_id: str) -> None:
        for _ in range(6):
            if api.encounter_state(encounter_id)["turn"] == "Thora":
                return
            api.encounter_advance(encounter_id)
        raise AssertionError("Thora never got a turn")

    def test_state_reports_positions_as_x_y_pairs(self) -> None:
        created = api.encounter_create([HERO, GOBLIN], seed=11)
        positions = {
            entry["name"]: entry["position"]
            for entry in created["state"]["combatants"]
        }
        assert positions == {"Thora": [0, 0], "Goblin": [5, 0]}

    def test_a_combatant_may_be_placed_at_an_x_y_position(self) -> None:
        goblin = {**GOBLIN, "position": [30, 40]}
        created = api.encounter_create([HERO, goblin], seed=11)
        placed = next(
            entry for entry in created["state"]["combatants"]
            if entry["name"] == "Goblin"
        )
        assert placed["position"] == [30, 40]

    def test_a_move_accepts_an_x_y_destination(self) -> None:
        created = api.encounter_create([HERO, {**GOBLIN, "position": 60}], seed=11)
        encounter_id = str(created["encounter_id"])
        self.advance_to_thora(encounter_id)
        acted = api.encounter_act(encounter_id, kind="move", to_position=[10, 5])
        moved = next(
            entry for entry in acted["state"]["combatants"]
            if entry["name"] == "Thora"
        )
        assert moved["position"] == [10, 5]

    def test_a_bad_position_pair_is_refused(self) -> None:
        with pytest.raises(api.ToolError, match=r"\[x, y\]"):
            api.encounter_create(
                [{**HERO, "position": [1, 2, 3]}, GOBLIN], seed=11
            )

    def test_an_unknown_movement_rule_lists_the_valid_ones(self) -> None:
        with pytest.raises(api.ToolError, match="5-10-5"):
            api.encounter_create([HERO, GOBLIN], seed=11, movement_rule="euclidean")

    def test_the_variant_diagonal_rule_is_accepted(self) -> None:
        created = api.encounter_create([HERO, GOBLIN], seed=11, movement_rule="5-10-5")
        assert created["state"]["round"] == 1


class TestMapTools:
    """The inline map spec: rows-and-legend authoring, features, and refusals."""

    CORRIDOR: dict[str, Any] = {
        "name": "corridor",
        "width": 4,
        "height": 3,
        "rows": [".#..", ".#..", ".#.."],
        "legend": {".": "normal", "#": "wall"},
        "features": [{"name": "door", "square": [1, 1]}],
    }

    def start(self) -> str:
        created = api.encounter_create(
            [HERO, {**GOBLIN, "position": [15, 0]}], seed=11, map=self.CORRIDOR
        )
        return str(created["encounter_id"])

    def advance_to_thora(self, encounter_id: str) -> None:
        for _ in range(6):
            if api.encounter_state(encounter_id)["turn"] == "Thora":
                return
            api.encounter_advance(encounter_id)
        raise AssertionError("Thora never got a turn")

    def test_a_created_map_appears_in_state(self) -> None:
        state = api.encounter_state(self.start())
        assert state["map"]["name"] == "corridor"
        assert state["map"]["width"] == 4
        assert state["map"]["height"] == 3
        assert state["map"]["features"]["door"] == {
            "square": [1, 1], "kind": "door", "open": False,
        }

    def test_interact_opens_the_door_over_the_wire(self) -> None:
        encounter_id = self.start()
        self.advance_to_thora(encounter_id)
        api.encounter_act(encounter_id, kind="move", to_position=[0, 5])
        acted = api.encounter_act(encounter_id, kind="interact", feature="door")
        assert acted["state"]["map"]["features"]["door"]["open"] is True

    def test_a_wall_refuses_the_move_with_the_reason(self) -> None:
        encounter_id = self.start()
        self.advance_to_thora(encounter_id)
        with pytest.raises(api.ToolError, match="no route"):
            api.encounter_act(encounter_id, kind="move", to_position=[10, 0])

    def test_an_unknown_map_key_is_refused(self) -> None:
        with pytest.raises(api.ToolError, match="unknown map key"):
            api.encounter_create(
                [HERO, GOBLIN], seed=1, map={**self.CORRIDOR, "tiles": []}
            )

    def test_a_row_of_the_wrong_width_is_refused(self) -> None:
        broken = {**self.CORRIDOR, "rows": [".#..", ".#.", "...."]}
        with pytest.raises(api.ToolError, match="row 1 is 3 characters"):
            api.encounter_create([HERO, GOBLIN], seed=1, map=broken)

    def test_a_character_missing_from_the_legend_is_refused(self) -> None:
        broken = {**self.CORRIDOR, "rows": [".#..", ".#..", "..~."]}
        with pytest.raises(api.ToolError, match="legend does not define"):
            api.encounter_create([HERO, GOBLIN], seed=1, map=broken)

    def test_rows_and_a_terrain_list_together_are_refused(self) -> None:
        broken = {**self.CORRIDOR, "terrain": []}
        with pytest.raises(api.ToolError, match="not both"):
            api.encounter_create([HERO, GOBLIN], seed=1, map=broken)

    def test_a_terrain_list_is_an_accepted_alternative(self) -> None:
        spec: dict[str, Any] = {
            "width": 4, "height": 1,
            "terrain": [{"kind": "difficult", "squares": [[2, 0]]}],
        }
        created = api.encounter_create(
            [HERO, {**GOBLIN, "position": [15, 0]}], seed=11, map=spec
        )
        assert created["state"]["map"]["width"] == 4

    def test_a_feature_off_the_map_is_refused(self) -> None:
        broken = {
            **self.CORRIDOR,
            "features": [{"name": "door", "square": [9, 9]}],
        }
        with pytest.raises(api.ToolError, match="outside the 4x3 map"):
            api.encounter_create([HERO, GOBLIN], seed=1, map=broken)

    def test_an_unknown_terrain_kind_is_refused_with_the_loaded_kinds(self) -> None:
        broken = {**self.CORRIDOR, "legend": {".": "normal", "#": "vale-lava"}}
        with pytest.raises(api.ToolError, match="vale-lava"):
            api.encounter_create(
                [HERO, {**GOBLIN, "position": [15, 0]}], seed=1, map=broken
            )

    def test_a_starting_position_inside_a_wall_is_refused(self) -> None:
        with pytest.raises(api.ToolError, match="impassable"):
            api.encounter_create(
                [{**HERO, "position": [5, 0]}, {**GOBLIN, "position": [15, 0]}],
                seed=1,
                map=self.CORRIDOR,
            )


class TestEncounterLog:
    def start(self, seed: int = 11) -> str:
        created = api.encounter_create([HERO, GOBLIN], seed=seed)
        return str(created["encounter_id"])

    def test_the_log_reports_its_seed_and_format(self) -> None:
        encounter_id = self.start(seed=42)
        result = api.encounter_log(encounter_id)
        assert result["encounter_id"] == encounter_id
        assert result["seed"] == 42
        assert result["format"] == "fivee-sim-log/1"

    def test_paging_walks_the_whole_log_without_loss(self) -> None:
        encounter_id = self.start()
        for _ in range(4):
            api.encounter_advance(encounter_id)
        full = api.encounter_log(encounter_id, include_actions=False)
        assert full["total_events"] > 2
        assert full["next"] is None

        paged: list[dict[str, object]] = []
        since = 0
        while True:
            page = api.encounter_log(encounter_id, since=since, limit=2,
                                     include_actions=False)
            assert len(page["events"]) <= 2
            paged.extend(page["events"])
            if page["next"] is None:
                break
            assert page["next"] == since + len(page["events"])
            since = page["next"]
        assert paged == full["events"]

    def test_events_are_stamped_with_their_position(self) -> None:
        encounter_id = self.start()
        api.encounter_advance(encounter_id)
        api.encounter_advance(encounter_id)  # wraps the round: seven events in all
        result = api.encounter_log(encounter_id, since=1, limit=3)
        assert [event["seq"] for event in result["events"]] == [1, 2, 3]

    def test_actions_appear_after_an_act_and_can_be_omitted(self) -> None:
        encounter_id = self.start()
        state = api.encounter_state(encounter_id)
        actor = str(state["turn"])
        target = "Goblin" if actor == "Thora" else "Thora"
        attack = "Longsword" if actor == "Thora" else "Scimitar"
        api.encounter_act(encounter_id, kind="attack", target=target, attack=attack)

        result = api.encounter_log(encounter_id)
        assert result["total_actions"] == len(result["actions"]) == 1
        record = result["actions"][0]
        assert record["actor"] == actor
        assert record["action"]["kind"] == "attack"
        assert record["action"]["target"] == target

        trimmed = api.encounter_log(encounter_id, include_actions=False)
        assert "actions" not in trimmed
        assert trimmed["total_actions"] == 1

    def test_an_unknown_id_lists_the_active_encounters(self) -> None:
        self.start()
        with pytest.raises(api.ToolError, match="active:"):
            api.encounter_log("enc-does-not-exist")

    def test_bad_paging_arguments_are_refused(self) -> None:
        encounter_id = self.start()
        with pytest.raises(api.ToolError, match="since"):
            api.encounter_log(encounter_id, since=-1)
        with pytest.raises(api.ToolError, match="limit"):
            api.encounter_log(encounter_id, limit=0)


class TestSpecValidation:
    def test_fewer_than_two_combatants_is_refused(self) -> None:
        with pytest.raises(api.ToolError, match="at least two"):
            api.encounter_create([HERO])

    def test_an_incomplete_custom_spec_says_what_is_missing(self) -> None:
        with pytest.raises(api.ToolError, match="missing"):
            api.encounter_create([{"name": "Nameless"}, GOBLIN])

    def test_an_unknown_bundled_monster_lists_the_available_ones(self) -> None:
        with pytest.raises(api.ToolError, match="Goblin Warrior"):
            api.encounter_create([HERO, {"monster": "Tarrasque"}])

    def test_an_attack_spec_missing_a_field_is_reported(self) -> None:
        broken = dict(HERO)
        broken["attacks"] = [{"name": "Club"}]
        with pytest.raises(api.ToolError, match="attack spec is missing"):
            api.encounter_create([broken, GOBLIN])


def map_document() -> dict[str, Any]:
    """A 5x4 room split by a wall, open along the bottom row."""
    return {
        "format": "fivee-sim-map",
        "format_version": 1,
        "name": "adapter chamber",
        "grid": {"width": 5, "height": 4, "cell_feet": 5},
        "legend": {".": "floor", "#": "wall"},
        "tiles": ["..#..", "..#..", "..#..", "....."],
        "features": [],
        "provenance": {
            "generator": "hand",
            "seed": 3,
            "params": {},
            "edited": False,
            "source": "Authored for the test suite; 5E-compatible original content",
        },
    }


class TestMapAdapters:
    """The map tools as thin shims: sessions, seeds, and error mapping."""

    def load(self) -> str:
        return str(api.map_load(document=map_document())["map_id"])

    def test_map_generate_reports_its_seed_and_reproduces(self) -> None:
        first = api.map_generate("dungeon", {"width": 24, "height": 20}, seed=9)
        second = api.map_generate("dungeon", {"width": 24, "height": 20}, seed=9)
        assert first["seed"] == second["seed"] == 9
        assert first["render"]["rows"] == second["render"]["rows"]
        assert first["params"]["width"] == 24
        assert first["params"]["min_room"] == 4  # defaults come back resolved
        assert first["provenance"]["edited"] is False
        rendered_one = api.map_render(str(first["map_id"]))
        rendered_two = api.map_render(str(second["map_id"]))
        assert rendered_one["rows"] == rendered_two["rows"]

    def test_map_generate_without_a_seed_still_reports_one(self) -> None:
        result = api.map_generate("caves", {"width": 12, "height": 10})
        assert isinstance(result["seed"], int)

    def test_an_unknown_kind_lists_the_valid_ones(self) -> None:
        with pytest.raises(api.ToolError, match="caves, dungeon, overland"):
            api.map_generate("labyrinth")

    def test_an_unknown_param_names_the_valid_keys(self) -> None:
        with pytest.raises(api.ToolError, match="min_room"):
            api.map_generate("dungeon", {"rooms": 9}, seed=1)

    def test_map_load_requires_exactly_one_source(self) -> None:
        with pytest.raises(api.ToolError, match="exactly one"):
            api.map_load()
        with pytest.raises(api.ToolError, match="exactly one"):
            api.map_load(path="/tmp/x.json", document=map_document())

    def test_map_load_reports_every_diagnostic(self) -> None:
        broken = map_document()
        broken["tiles"][0] = "..?.."
        broken["grid"]["depth"] = 3
        with pytest.raises(api.ToolError, match="2 map error"):
            api.map_load(document=broken)

    def test_an_unknown_map_id_lists_the_active_ones(self) -> None:
        self.load()
        with pytest.raises(api.ToolError, match="active:"):
            api.map_render("map-does-not-exist")

    def test_save_then_load_by_path_round_trips(self, tmp_path: Path) -> None:
        map_id = self.load()
        target = str(tmp_path / "chamber.json")
        saved = api.map_save(map_id, path=target)
        assert saved["path"] == target
        reloaded = api.map_load(path=target)
        assert reloaded["sha256"] == saved["sha256"]
        assert reloaded["name"] == "adapter chamber"
        assert reloaded["warnings"] == []

    def test_map_save_defaults_into_the_maps_root_and_refuses_overwrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIVEE_SIM_MAPS", str(tmp_path))
        map_id = self.load()
        saved = api.map_save(map_id)
        assert saved["path"] == str(tmp_path / "adapter-chamber.json")
        with pytest.raises(api.ToolError, match="overwrite"):
            api.map_save(map_id)
        assert api.map_save(map_id, overwrite=True)["sha256"] == saved["sha256"]

    def test_map_load_replace_rebinds_the_id_and_bumps_the_generation(self) -> None:
        map_id = self.load()
        renamed = map_document()
        renamed["name"] = "replacement"
        result = api.map_load(document=renamed, replace=map_id)
        assert result["map_id"] == map_id
        assert api.map_render(map_id)["generation"] == 2

    def test_map_edit_is_atomic_over_the_wire(self) -> None:
        map_id = self.load()
        before = api.map_render(map_id)
        with pytest.raises(api.ToolError, match="operation #1"):
            api.map_edit(map_id, [
                {"op": "paint", "cells": [[0, 0]], "terrain": "wall"},
                {"op": "paint", "cells": [[99, 99]], "terrain": "wall"},
            ])
        after = api.map_render(map_id)
        assert after["generation"] == before["generation"]
        assert after["rows"] == before["rows"]

    def test_map_edit_applies_bumps_and_marks_edited(self) -> None:
        map_id = self.load()
        result = api.map_edit(map_id, [
            {"op": "paint", "cells": [[0, 0]], "terrain": "wall"},
        ])
        assert result["applied"] == 1
        assert result["generation"] == 2
        assert result["edited"] is True
        assert result["summary"]["terrain_counts"] == {"floor": 16, "wall": 4}
        assert result["render"]["rows"][0] == "#.#.."

    def test_map_edit_parity_with_the_service_layer(self) -> None:
        operations: list[dict[str, Any]] = [
            {"op": "set_terrain", "rect": [3, 0, 2, 2], "terrain": "wall"},
            {"op": "set_name", "name": "walled up"},
        ]
        terrain = api._registry().terrain_effects
        document = parse_document(map_document(), source="parity", terrain=terrain)
        expected = map_service.apply_edits(document, operations, terrain=terrain)

        map_id = self.load()
        result = api.map_edit(map_id, operations)
        assert result["summary"]["width"] == expected.grid.width
        assert result["summary"]["height"] == expected.grid.height
        assert result["summary"]["features"] == len(expected.features)
        counts: dict[str, int] = {}
        for row in expected.tiles:
            for char in row:
                kind = expected.legend[char]
                counts[kind] = counts.get(kind, 0) + 1
        assert result["summary"]["terrain_counts"] == counts
        assert result["render"]["rows"] == map_service.render_ascii(expected)["rows"]

    def test_map_query_answers_distance_sight_and_path(self) -> None:
        map_id = self.load()
        distance = api.map_query(map_id, "distance", frm=[0, 1], to=[4, 1])
        assert distance["feet"] == 20
        sight = api.map_query(map_id, "line_of_sight", frm=[0, 1], to=[4, 1])
        assert sight["line_of_sight"] is False
        path = api.map_query(map_id, "path", frm=[0, 1], to=[4, 1])
        assert path["reachable"] is True
        assert path["cost_feet"] == 20

    def test_map_query_refusals_name_the_valid_choices(self) -> None:
        map_id = self.load()
        with pytest.raises(api.ToolError, match="distance, line_of_sight, path"):
            api.map_query(map_id, "cover", frm=[0, 0], to=[1, 1])
        with pytest.raises(api.ToolError, match=r"\[x, y\]"):
            api.map_query(map_id, "distance", frm=[0], to=[1, 1])
        with pytest.raises(api.ToolError, match="outside the 5x4 map"):
            api.map_query(map_id, "distance", frm=[0, 0], to=[9, 9])


class TestUvttExport:
    """uvtt_export: always a file on disk, never an inlined payload."""

    def load(self) -> str:
        return str(api.map_load(document=map_document())["map_id"])

    def test_export_writes_the_file_and_the_counts_match(self, tmp_path: Path) -> None:
        map_id = self.load()
        target = str(tmp_path / "chamber.uvtt")
        result = api.uvtt_export(map_id, path=target, pixels_per_grid=8)
        assert result["path"] == target
        assert result["map_id"] == map_id
        assert result["image"] is True
        written = json.loads((tmp_path / "chamber.uvtt").read_text(encoding="utf-8"))
        assert result["bytes"] == (tmp_path / "chamber.uvtt").stat().st_size
        assert result["wall_polylines"] == len(written["line_of_sight"]) == 1
        assert result["portals"] == len(written["portals"]) == 0
        assert result["resolution"] == written["resolution"]
        assert written["resolution"]["map_size"] == {"x": 5.0, "y": 4.0}
        assert "bundle" not in result and "payload" not in result

    def test_the_default_path_is_the_maps_root_and_overwrite_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIVEE_SIM_MAPS", str(tmp_path))
        map_id = self.load()
        first = api.uvtt_export(map_id, pixels_per_grid=8, include_image=False)
        assert first["path"] == str(tmp_path / "uvtt" / "adapter-chamber.uvtt")
        # A derived artifact, like replay_export's files: re-export replaces it.
        second = api.uvtt_export(map_id, pixels_per_grid=8, include_image=False)
        assert second["path"] == first["path"]
        assert json.loads(Path(first["path"]).read_text(encoding="utf-8"))["image"] == ""

    def test_an_unknown_map_id_lists_the_active_ones(self) -> None:
        self.load()
        with pytest.raises(api.ToolError, match="active:"):
            api.uvtt_export("map-does-not-exist")

    def test_an_oversized_image_is_refused_with_the_remedy(self, tmp_path: Path) -> None:
        map_id = self.load()
        with pytest.raises(api.ToolError, match="lower pixels_per_grid"):
            api.uvtt_export(map_id, path=str(tmp_path / "big.uvtt"), pixels_per_grid=1000)


class TestEncountersOnLoadedMaps:
    """encounter_create(map_id=...): capture by value, staleness made visible."""

    def combatants(self) -> list[dict[str, Any]]:
        return [dict(HERO), {**GOBLIN, "position": [15, 0]}]

    def test_a_fight_captures_the_map_and_reports_its_source(self) -> None:
        map_id = str(api.map_load(document=map_document())["map_id"])
        created = api.encounter_create(self.combatants(), seed=11, map_id=map_id)
        source = created["map_source"]
        assert source["map_id"] == map_id
        assert source["map_generation"] == 1
        assert len(source["map_sha256"]) == 64
        assert created["state"]["map"]["width"] == 5

        state = api.encounter_state(str(created["encounter_id"]))
        assert state["map_source"] == {
            "map_id": map_id,
            "generation": 1,
            "current_generation": 1,
            "stale": False,
        }

    def test_staleness_flips_after_a_map_edit(self) -> None:
        map_id = str(api.map_load(document=map_document())["map_id"])
        created = api.encounter_create(self.combatants(), seed=11, map_id=map_id)
        encounter_id = str(created["encounter_id"])
        api.map_edit(map_id, [{"op": "paint", "cells": [[4, 3]], "terrain": "wall"}])
        source = api.encounter_state(encounter_id)["map_source"]
        assert source["stale"] is True
        assert source["current_generation"] == 2
        # The fight itself still resolves on the map it captured.
        assert api.encounter_state(encounter_id)["map"]["width"] == 5

    def test_an_inline_map_and_a_map_id_together_are_refused(self) -> None:
        map_id = str(api.map_load(document=map_document())["map_id"])
        with pytest.raises(api.ToolError, match="not both"):
            api.encounter_create(
                self.combatants(), seed=1,
                map={"width": 2, "height": 2}, map_id=map_id,
            )

    def test_a_mapless_fight_reports_no_map_source(self) -> None:
        created = api.encounter_create([HERO, GOBLIN], seed=1)
        assert "map_source" not in created
        assert api.encounter_state(str(created["encounter_id"]))["map_source"] is None

    def test_map_render_overlays_the_encounters_combatants(self) -> None:
        map_id = str(api.map_load(document=map_document())["map_id"])
        created = api.encounter_create(self.combatants(), seed=11, map_id=map_id)
        rendered = api.map_render(map_id, encounter_id=str(created["encounter_id"]))
        assert set(rendered["tokens"].values()) == {"Thora", "Goblin"}
        # Thora stands at square (0, 0), the goblin at (3, 0); letters follow
        # initiative order, whatever it rolled.
        row = rendered["rows"][0]
        assert {row[0], row[3]} == set(rendered["tokens"])
        assert row[2] == "#"

    def test_simulate_rounds_accepts_a_map_id_and_reports_the_source(self) -> None:
        map_id = str(api.map_load(document=map_document())["map_id"])
        result = api.simulate_rounds(
            self.combatants(), iterations=5, seed=7, max_rounds=10, map_id=map_id
        )
        assert sum(result["wins"].values()) == 5
        assert result["map_source"]["map_id"] == map_id
        assert result["map_source"]["map_generation"] == api.map_render(map_id)["generation"]


class TestAnalyticsTools:
    def test_simulate_rounds_reports_win_rates(self) -> None:
        result = api.simulate_rounds([HERO, GOBLIN], iterations=30, seed=7, max_rounds=15)
        assert sum(result["wins"].values()) == 30
        assert pytest.approx(sum(result["win_rate"].values()), abs=1e-6) == 1.0

    def test_simulate_dpr_reports_damage_per_round(self) -> None:
        result = api.simulate_dpr(HERO, target_ac=15, rounds=3, iterations=100, seed=7)
        assert result["damage"]["mean"] > 0
        assert result["damage_per_round"] > 0

    def test_bad_iteration_counts_are_refused(self) -> None:
        with pytest.raises(api.ToolError, match="at least 1"):
            api.simulate_rounds([HERO, GOBLIN], iterations=0)
