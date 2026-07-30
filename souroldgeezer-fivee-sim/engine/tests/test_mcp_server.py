"""Adapter tests for the MCP tool surface.

These exercise the tools as callables. The protocol handshake itself is checked
separately by ``scripts/check-mcp-handshake.py``, which speaks real JSON-RPC over
stdio; here the concern is input validation, seed reporting, and that state moves
through the session correctly.
"""

from __future__ import annotations

from typing import Any

import pytest

from fivee_sim.mcp_server import server as api

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
        assert any("Undead Fortitude" in note for note in result["unmodelled"])

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
        api.encounter_advance(encounter_id)  # wraps the round: five events in all
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
