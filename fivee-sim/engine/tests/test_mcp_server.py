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
    def test_no_topic_lists_everything_bundled(self) -> None:
        listing = api.lookup_rule()
        assert "prone" in listing["conditions"]
        assert "Fireball" in listing["spells"]
        assert "Goblin Warrior" in listing["monsters"]

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

    def test_a_monster_returns_its_record_including_unmodelled_traits(self) -> None:
        result = api.lookup_rule("zombie")
        assert result["kind"] == "monster"
        assert result["ac"] == 8
        assert any("Undead Fortitude" in note for note in result["unmodelled"])

    def test_a_miss_explains_that_only_srd_content_ships(self) -> None:
        with pytest.raises(api.ToolError, match="only SRD 5.2 content"):
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
