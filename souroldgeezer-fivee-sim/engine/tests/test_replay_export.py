"""``replay_export``: a fight becomes a file the replay viewer can play.

Four contracts are pinned here. The bundle's schema is what the viewer page
codes against, so its keys are asserted literally. The 64 KB result-size rule
works both ways — small bundles inline, large ones to disk. The embedded
export fills the viewer's data slot exactly once and leaves no empty slot
behind. And the map travels **by value**: an edit made after the encounter
was created must never change what an export replays on.
"""

from __future__ import annotations

import json
import re
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from fivee_sim import __version__
from fivee_sim.content import builtin_registry
from fivee_sim.kernel.actions import AttackKind, RiderExpiry
from fivee_sim.kernel.dice import Dice
from fivee_sim.kernel.grid import TERRAIN
from fivee_sim.kernel.rules import Ability, DamageType, Size
from fivee_sim.map_document import (
    MapDocument,
    MapFeatureRecord,
    as_payload,
    parse_document,
)
from fivee_sim.map_types import FeatureTrigger, TerrainPair, TriggerMode
from fivee_sim.model.creature import AttackOption
from fivee_sim.model.encounter import EncounterMode
from fivee_sim.service import map_ops, specs
from fivee_sim.service import replay as replay_service
from fivee_sim.service.errors import NotFoundError, RequestError

from . import api
from .conftest import (
    REPLAY_GOBLIN,
    REPLAY_HERO,
    advance_encounter_to,
    mapless_fight,
)

FIXTURE = "Authored for the test suite; 5E-compatible original content"


def chamber() -> dict[str, Any]:
    """A walled room with an open east door and a stair, for mapped fights."""
    return {
        "format": "fivee-sim-map",
        "format_version": 1,
        "name": "replay chamber",
        "grid": {"width": 6, "height": 5, "cell_feet": 5},
        "legend": {".": "floor", "#": "wall"},
        "tiles": [
            "######",
            "#....#",
            "#.....",
            "#....#",
            "######",
        ],
        "features": [
            {
                "id": "door-east",
                "kind": "door",
                "at": [5, 2],
                "orientation": "vertical",
                "state": "open",
            },
            {"id": "stair-1", "kind": "stairs_down", "at": [1, 3]},
        ],
        "provenance": {
            "generator": "hand",
            "seed": 11,
            "params": {},
            "edited": False,
            "source": FIXTURE,
        },
    }


def mapped_fight(seed: int = 43) -> tuple[str, str]:
    # A map is a file; saving it under an id is how a fight can name it.
    map_id = "replay-chamber"
    api.map_save(map_id, chamber())
    created = api.encounter_create(
        [dict(REPLAY_HERO), dict(REPLAY_GOBLIN)], seed=seed, map_id=map_id
    )
    return str(created["encounter_id"]), map_id


class TestBundleSchema:
    def test_the_bundle_carries_exactly_the_viewer_contract(self) -> None:
        encounter_id = mapless_fight()
        result = api.replay_export(encounter_id, format_version=1)
        bundle = result["bundle"]
        assert set(bundle) == {
            "format", "format_version", "name", "seed", "map", "initial", "events",
        }
        assert bundle["format"] == "fivee-sim-replay"
        assert bundle["format_version"] == 1
        assert bundle["seed"] == result["seed"]
        assert set(bundle["initial"]) == {"creatures", "map_open_features"}
        for creature in bundle["initial"]["creatures"]:
            assert set(creature) == {"name", "team", "position", "hp", "max_hp"}
            assert len(creature["position"]) == 2
        assert {c["name"] for c in bundle["initial"]["creatures"]} == {"Thora", "Goblin"}
        json.dumps(bundle)  # the whole thing must be plain JSON

    def test_initial_positions_are_the_starting_ones_not_the_current(self) -> None:
        encounter_id = mapless_fight()
        state = api.encounter_state(encounter_id)
        mover = str(state["turn"])
        api.encounter_act(encounter_id, "move", to_position=[30, 25])
        bundle = api.replay_export(encounter_id, format_version=1)["bundle"]
        starts = {c["name"]: c["position"] for c in bundle["initial"]["creatures"]}
        assert starts[mover] in ([5, 5], [15, 15])
        moves = [e for e in bundle["events"] if e["kind"] == "move"]
        assert moves, "the move must be in the exported log"
        # In-process the payload holds a tuple; over the wire it is a JSON list.
        assert list(moves[-1]["data"]["destination"]) == [30, 25]

    def test_the_events_are_the_whole_log_so_far(self) -> None:
        encounter_id = mapless_fight()
        api.encounter_advance(encounter_id)
        bundle = api.replay_export(encounter_id, format_version=1)["bundle"]
        log = api.encounter_log(encounter_id, include_actions=False)
        assert len(bundle["events"]) == log["total_events"]
        assert bundle["events"][0]["kind"] == "round"

    def test_an_unknown_encounter_is_refused(self) -> None:
        with pytest.raises(NotFoundError, match="unknown encounter"):
            api.replay_export("enc-never")


class TestBundleV2:
    def test_v2_is_the_default_export_contract(self) -> None:
        assert api.replay_export(mapless_fight())["bundle"]["format_version"] == 2

    def test_v2_carries_the_reconstruction_and_state_contract(self) -> None:
        encounter_id = mapless_fight(seed=67)

        bundle = api.replay_export(encounter_id, format_version=2)["bundle"]

        assert bundle["format_version"] == 2
        assert bundle["engine_version"] == __version__
        assert bundle["encounter"] == {
            "id": encounter_id,
            "seed": 67,
            "movement_rule": "5-5-5",
            # Which kind of chapter it was. Written whole rather than as a
            # membership check so the block stays exhaustive: a field added to
            # what a bundle says about its encounter is a decision somebody has
            # to record here.
            "mode": EncounterMode.COMBAT.value,
        }
        assert bundle["initial"]["state"]["order"] == [
            creature["name"] for creature in bundle["initial"]["creatures"]
        ]
        assert bundle["latest_state"] == api.encounter_state(encounter_id)
        assert bundle["actions"] == []

    def test_v2_captures_an_inline_map_instead_of_using_the_neutral_plane(self) -> None:
        inline = {
            "name": "inline room",
            "width": 6,
            "height": 5,
            "rows": ["######", "#....#", "#....#", "#....#", "######"],
            "legend": {"#": "wall", ".": "normal"},
            "features": [
                {
                    "name": "door-east",
                    "square": [5, 2],
                    "orientation": "vertical",
                    "initially_open": True,
                }
            ],
        }
        created = api.encounter_create(
            [dict(REPLAY_HERO), dict(REPLAY_GOBLIN)], seed=71, map=inline
        )

        bundle = api.replay_export(
            str(created["encounter_id"]), format_version=2
        )["bundle"]

        assert bundle["map"]["format"] == "fivee-sim-map"
        assert bundle["map"]["name"] == "inline room"
        assert bundle["map"]["legend"][bundle["map"]["tiles"][0][0]] == "wall"
        assert bundle["map"]["legend"][bundle["map"]["tiles"][1][1]] == "normal"
        assert bundle["initial"]["map_open_features"] == ["door-east"]
        # The assertion whose absence hid a real defect for as long as it
        # existed. Everything above reads keys off the payload, and a payload
        # can carry every one of them and still not be a map: an inline spec
        # could not say how its door hung, so this bundle was a document the
        # parser refused — and `replay.validate_replay` checks the map's *shape*
        # without ever parsing it, so it called the bundle valid. A caller who
        # exported this could not open it.
        parse_document(bundle["map"], source="bundle", terrain=TERRAIN)

    def test_a_captured_map_keeps_trigger_definitions(self) -> None:
        """A fixture's predicate survives the capture and reads back as itself.

        This case used to hold ``replay.battle_map_payload``, which re-synthesised
        a document out of a runtime battle map because a spec could not produce
        one. That fake is gone: every producer builds a
        :class:`~fivee_sim.map_document.MapDocument` now, so the capture is
        ``as_payload`` and the thing under test is the format's own writer. The
        claim is unchanged and is the one that matters — a trigger written into a
        bundle parses back to the trigger it was, so a replay of a fight with a
        pressure plate in it is a replay of that fight.
        """
        document = MapDocument.flat(
            name="trigger hall",
            width=3,
            height=1,
            default_terrain="floor",
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

        payload = as_payload(document)

        gate = next(feature for feature in payload["features"] if feature["id"] == "gate")
        assert gate["trigger"] == {
            "when": {"lever": "open"},
            "set": "open",
            "mode": "maintained",
        }
        parsed = parse_document(payload, source="replay", terrain=TERRAIN)
        assert next(feature for feature in parsed.features if feature.id == "gate").trigger == (
            document.fixtures()["gate"].trigger
        )

    def test_v2_records_normalized_inputs_actions_checkpoints_and_integrity(self) -> None:
        encounter_id = mapless_fight(seed=73)
        actor = str(api.encounter_state(encounter_id)["turn"])

        api.encounter_act(encounter_id, "move", to_position=[25, 20])
        bundle = api.replay_export(encounter_id, format_version=2)["bundle"]

        normalized = {entry["name"]: entry for entry in bundle["initial"]["combatants"]}
        assert normalized["Thora"]["speed"] == 30
        assert normalized["Thora"]["attacks"][0]["damage"] == "1d8+3"
        assert normalized["Goblin"]["provenance"]
        assert bundle["actions"][0]["actor"] == actor
        assert bundle["actions"][0]["action"]["kind"] == "move"
        assert len(bundle["checkpoints"]) == 2
        assert bundle["checkpoints"][0]["event_count"] == 2
        assert bundle["checkpoints"][-1]["state"] == bundle["latest_state"]
        assert all(event["timestamp"] for event in bundle["events"])
        assert bundle["content"]["sha256"]
        assert bundle["integrity"]["algorithm"] == "sha256"
        assert replay_service.validate_replay(bundle) == []

    def test_a_ruling_the_table_made_leaves_a_bundle_that_still_plays(self) -> None:
        """The third mutator, held to what the other two are held to.

        ``encounter.condition`` changes the fight — ``recover_session`` replays
        it beside ``act`` and ``advance`` for exactly that reason — so the
        bundle it leaves owes the same two invariants as theirs: every event
        stamped, and the last checkpoint equal to the state the bundle reports.
        It owed them and did not pay: the live path stamped nothing and captured
        nothing, so a fight in which anybody had imposed a condition exported a
        file ``validate_replay`` refuses, and ``adventure.replay`` refused the
        whole run with it.

        The asymmetry is the tell and is why this is asserted here rather than
        left to the composition tests: drop the session between the ruling and
        the export and the same fight comes back playable, because recovery
        does what the live call skipped.
        """
        encounter_id = mapless_fight(seed=97)

        api.encounter_condition(encounter_id, "Goblin", "prone")
        bundle = api.replay_export(encounter_id, format_version=2)["bundle"]

        assert all(event["timestamp"] for event in bundle["events"])
        assert bundle["checkpoints"][-1]["state"] == bundle["latest_state"]
        assert replay_service.validate_replay(bundle) == []

    def test_v2_normalized_inputs_preserve_playtest_mechanics(self) -> None:
        stirge = dict(REPLAY_HERO)
        stirge.update(
            {
                "name": "Stirge",
                "team": "monsters",
                "position": [15, 15],
                "climb_speed": 10,
                "swim_speed": 15,
                "fly_speed": 40,
                "terrain_cost_overrides": ["grain"],
                "darkvision": 60,
                "blindsight": 10,
                "death_rule": "instant",
                "bonus_actions": ["disengage"],
                "surrender_when_last": True,
                "redirect_attack": True,
                "arrival_round": 2,
                "attacks": [
                    {
                        "name": "Proboscis",
                        "attack_bonus": 5,
                        "damage": "1d6+3",
                        "damage_type": "piercing",
                        "advantage_bonus_damage": "1d6",
                        "advantage_bonus_with_adjacent_ally": True,
                        "on_hit_attach": True,
                        "attached_damage": "2d4",
                        "attached_damage_type": "necrotic",
                        "detach_after_damage": 10,
                    }
                ],
            }
        )
        created = api.encounter_create([dict(REPLAY_HERO), stirge], seed=79)

        bundle = api.replay_export(
            str(created["encounter_id"]), format_version=2
        )["bundle"]
        captured = next(
            entry
            for entry in bundle["initial"]["combatants"]
            if entry["name"] == "Stirge"
        )

        assert captured["climb_speed"] == 10
        assert captured["swim_speed"] == 15
        assert captured["fly_speed"] == 40
        assert captured["terrain_cost_overrides"] == ["grain"]
        assert captured["darkvision"] == 60
        assert captured["blindsight"] == 10
        assert captured["death_rule"] == "instant"
        assert captured["bonus_actions"] == ["disengage"]
        assert captured["surrender_when_last"] is True
        assert captured["redirect_attack"] is True
        assert captured["arrival_round"] == 2
        assert captured["attacks"][0]["advantage_bonus_with_adjacent_ally"] is True
        assert captured["attacks"][0]["attached_damage"] == "2d4"
        assert captured["attacks"][0]["detach_after_damage"] == 10

    def test_unknown_replay_versions_are_refused(self) -> None:
        with pytest.raises(RequestError, match="format_version must be 1 or 2"):
            api.replay_export(mapless_fight(), format_version=99)

    def test_v2_state_checkpoints_include_transient_turn_and_effect_state(self) -> None:
        encounter_id = mapless_fight(seed=89)

        state = api.replay_export(encounter_id, format_version=2)["bundle"][
            "initial"
        ]["state"]

        assert state["movement_rule"] == "5-5-5"
        assert state["ongoing_effects"] == []
        for combatant in state["combatants"]:
            assert set(("reaction_available", "disengaged")) <= set(combatant)

    def test_v2_preserves_storeys_and_cross_storey_actions(self) -> None:
        document = {
            "format": "fivee-sim-map",
            "format_version": 1,
            "name": "two floors",
            "grid": {"width": 5, "height": 4, "cell_feet": 5},
            "legend": {".": "floor"},
            "tiles": [".....", ".....", ".....", "....."],
            "features": [
                {"id": "stair-foot", "kind": "stairs_up", "at": [0, 3], "to_level": 1}
            ],
            "levels": [
                {
                    "index": 1,
                    "name": "gallery",
                    "tiles": [".....", ".....", ".....", "....."],
                    "elevation": {"default": 10, "squares": []},
                    "features": [
                        {
                            "id": "stair-head",
                            "kind": "stairs_down",
                            "at": [0, 3],
                            "to_level": 0,
                        }
                    ],
                }
            ],
            "provenance": {
                "generator": "hand",
                "seed": 1,
                "params": {},
                "edited": False,
                "source": FIXTURE,
            },
        }
        map_id = "storeyed-chamber"
        api.map_save(map_id, document)
        created = api.encounter_create(
            [
                dict(REPLAY_HERO, position=[0, 15]),
                dict(REPLAY_GOBLIN, position=[20, 15]),
            ],
            seed=97,
            map_id=map_id,
        )
        encounter_id = str(created["encounter_id"])
        advance_encounter_to(encounter_id, "Thora")
        api.encounter_act(
            encounter_id, "move", to_position=[0, 15], to_level=1
        )

        bundle = api.replay_export(encounter_id, format_version=2)["bundle"]

        assert bundle["map"]["levels"][0]["index"] == 1
        move = next(event for event in bundle["events"] if event["kind"] == "move")
        assert (move["data"]["from_level"], move["data"]["to_level"]) == (0, 1)
        assert "level 0" in move["detail"]
        assert "level 1" in move["detail"]
        thora = next(
            creature
            for creature in bundle["latest_state"]["combatants"]
            if creature["name"] == "Thora"
        )
        assert thora["level"] == 1


class TestSizeGate:
    def test_a_small_bundle_comes_back_inline(self) -> None:
        result = api.replay_export(mapless_fight())
        assert "bundle" in result
        assert "path" not in result
        assert result["bytes"] <= map_ops.INLINE_BUNDLE_BYTES

    def test_a_large_bundle_goes_to_disk_at_the_default_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default path is the *replays* root, which maps never decide.

        This used to read ``FIVEE_SIM_MAPS`` and land under the maps root.
        Replays are rooted independently now, so a maps root pointed somewhere
        exotic no longer drags the fight records along with it — and the
        assertion below names the replays root to say so.
        """
        replays_dir = tmp_path / "replays"
        monkeypatch.setenv("FIVEE_SIM_MAPS", str(tmp_path / "maps"))
        monkeypatch.setenv("FIVEE_SIM_REPLAYS", str(replays_dir))
        monkeypatch.setattr(map_ops, "INLINE_BUNDLE_BYTES", 64)
        encounter_id = mapless_fight(seed=47)
        result = api.replay_export(encounter_id)
        assert "bundle" not in result
        assert result["path"] == str(
            replays_dir / f"{encounter_id}-{result['seed']}.json"
        )
        text = Path(str(result["path"])).read_text(encoding="utf-8")
        assert result["bytes"] == len(text.encode("utf-8"))
        parsed = json.loads(text)
        assert parsed["format"] == "fivee-sim-replay"
        assert parsed["seed"] == result["seed"]

    def test_an_explicit_path_writes_even_a_small_bundle(self, tmp_path: Path) -> None:
        target = tmp_path / "exports" / "duel.json"
        result = api.replay_export(mapless_fight(), path=str(target))
        assert result["path"] == str(target)
        assert "sha256" in result
        assert json.loads(target.read_text(encoding="utf-8"))["name"] == result["encounter_id"]

    def test_every_export_reports_the_hash_of_the_exact_written_bytes(
        self, tmp_path: Path
    ) -> None:
        encounter_id = mapless_fight()
        json_result = api.replay_export(
            encounter_id, path=str(tmp_path / "fight.json"), format_version=2
        )
        html_result = api.replay_export(
            encounter_id, path=str(tmp_path / "fight.html"), embed=True, format_version=2
        )

        assert json_result["sha256"] == replay_service.sha256_bytes(
            (tmp_path / "fight.json").read_bytes()
        )
        assert html_result["sha256"] == replay_service.sha256_bytes(
            (tmp_path / "fight.html").read_bytes()
        )


class TestEmbed:
    def slot_content(self, html: str) -> Any:
        found = re.findall(
            r'<script type="application/json" id="embedded-data">(.*?)</script>',
            html,
            re.DOTALL,
        )
        assert len(found) == 1, "exactly one embedded-data slot must remain"
        return json.loads(found[0])

    def test_embed_fills_the_slot_once_and_leaves_no_null_slot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIVEE_SIM_MAPS", str(tmp_path))
        encounter_id = mapless_fight(seed=53)
        inline = api.replay_export(encounter_id)["bundle"]
        result = api.replay_export(encounter_id, embed=True)
        html = Path(str(result["path"])).read_text(encoding="utf-8")
        assert result["path"].endswith(f"-{result['seed']}.html")
        assert replay_service.EMBED_SLOT not in html, "the null slot must be filled"
        assert self.slot_content(html) == inline
        # Standalone means standalone: the renderer rides inside the file
        # rather than being fetched from a server that is not there.
        assert replay_service.RENDERER_TAG not in html
        assert "var FiveeRenderer" in html
        assert result["bytes"] == len(html.encode("utf-8"))

    def test_embedded_prose_cannot_close_the_script_block(self, tmp_path: Path) -> None:
        # An event detail could legitimately contain angle brackets; the
        # embedding must escape them so the JSON cannot end the script tag.
        viewer = (
            "<!doctype html><script>/*x*/</script>"
            + replay_service.EMBED_SLOT
            + replay_service.RENDERER_TAG
        )
        bundle_json = json.dumps({"detail": "a </script> in prose"})
        filled = replay_service.embed_in_viewer(viewer, bundle_json)
        assert "</script> in prose" not in filled
        assert "\\u003c/script> in prose" in filled

    def test_a_page_without_the_slot_is_refused(self) -> None:
        with pytest.raises(ValueError, match="exactly once"):
            replay_service.embed_in_viewer("<!doctype html>", "{}")

    def test_embed_honours_an_explicit_path(self, tmp_path: Path) -> None:
        target = tmp_path / "show.html"
        result = api.replay_export(mapless_fight(), path=str(target), embed=True)
        assert result["path"] == str(target)
        assert target.read_text(encoding="utf-8").startswith("<!doctype html")


class TestMapCapture:
    def test_a_mapped_fight_carries_the_document_it_was_created_on(self) -> None:
        encounter_id, map_id = mapped_fight()
        bundle = api.replay_export(encounter_id, format_version=1)["bundle"]
        assert bundle["name"] == "replay chamber"
        assert bundle["map"]["name"] == "replay chamber"
        assert bundle["map"]["tiles"] == chamber()["tiles"]
        assert bundle["initial"]["map_open_features"] == ["door-east"]

    def test_an_edit_after_creation_never_reaches_the_export(self) -> None:
        # Staleness immunity: the fight captured the document by value, so a
        # map_edit that lands between creation and export changes nothing.
        encounter_id, map_id = mapped_fight(seed=59)
        api.map_edit(map_id, [
            {"op": "paint", "cells": [[1, 1], [2, 1]], "terrain": "wall"},
            {"op": "set_name", "name": "renovated chamber"},
        ])
        bundle = api.replay_export(encounter_id, format_version=1)["bundle"]
        assert bundle["map"]["name"] == "replay chamber"
        assert bundle["map"]["tiles"] == chamber()["tiles"]
        assert bundle["map"]["provenance"]["edited"] is False

    def test_an_inline_map_fight_replays_on_the_neutral_plane(self) -> None:
        created = api.encounter_create(
            [dict(REPLAY_HERO), dict(REPLAY_GOBLIN)],
            seed=61,
            map={
                "width": 6, "height": 5,
                "rows": ["......", "......", "......", "......", "......"],
                "legend": {".": "normal"},
            },
        )
        bundle = api.replay_export(
            str(created["encounter_id"]), format_version=1
        )["bundle"]
        assert bundle["map"] is None

    def test_a_mapless_fight_carries_no_map(self) -> None:
        bundle = api.replay_export(mapless_fight(), format_version=1)["bundle"]
        assert bundle["map"] is None
        assert bundle["initial"]["map_open_features"] == []


def test_a_combatants_facing_is_accepted_reported_and_carried_into_the_bundle() -> None:
    # Three claims in one fight because they are one chain: the spec key sets
    # must accept it (they refused it outright before facing existed), the state
    # payload must report it, and the bundle's state slots inherit that payload
    # wholesale — so a facing a caller authored survives an export with no
    # separate serialisation to keep in step.
    hero = dict(REPLAY_HERO)
    hero["facing"] = "northeast"
    encounter_id = str(
        api.encounter_create([hero, dict(REPLAY_GOBLIN)], seed=121)["encounter_id"]
    )

    live = next(
        entry for entry in api.encounter_state(encounter_id)["combatants"]
        if entry["name"] == "Thora"
    )
    bundle = api.replay_export(encounter_id, format_version=2)["bundle"]
    checkpointed = next(
        entry for entry in bundle["latest_state"]["combatants"]
        if entry["name"] == "Thora"
    )

    assert live["facing"] == "northeast"
    assert checkpointed["facing"] == "northeast"
    assert "facing" not in next(
        entry for entry in bundle["latest_state"]["combatants"]
        if entry["name"] == "Goblin"
    )


def test_the_journal_captures_every_key_a_combatant_spec_accepts() -> None:
    # One contract read in two directions. `creature_from_spec` says what a
    # caller may state about a combatant; `normalized_combatant_payload` says
    # what gets journalled; and `recover_session` feeds the second straight
    # back into the first. A key in one set and not the other is state a
    # recovered fight drops on the floor — and an omitted key is not an
    # unknown key, so nothing refuses it and nothing else goes red.
    #
    # Derived from both declarations rather than listed here: a literal set
    # would pin each side against a copy of itself, and the two defects this
    # test exists for — `facing`, then the four carry-over keys — were both
    # added to one side only while every other case stayed green.
    creature = specs.creature_from_spec(
        {"name": "Thora", "team": "party", "ac": 16, "max_hp": 30, "position": [0, 0]},
        builtin_registry(),
    )
    assert set(replay_service.normalized_combatant_payload(creature)) == (
        specs.DESCRIBED_SPEC_KEYS
    )


def test_a_fight_recovered_from_its_journal_is_still_stabilised_not_dying() -> None:
    # The consequence, in the terms the rules care about. `dying` is derived as
    # `not dead and hp == 0 and not stable`, so losing `stable` alone flips a
    # stabilised combatant back to dying — the same silent state flip the spec
    # keys were widened to prevent, arriving by way of the journal instead.
    hero = dict(REPLAY_HERO) | {
        "hp": 0, "stable": True, "death_saves": {"successes": 3, "failures": 1},
    }
    encounter_id = str(
        api.encounter_create([hero, dict(REPLAY_GOBLIN)], seed=126)["encounter_id"]
    )

    def thora(state: dict[str, Any]) -> dict[str, Any]:
        return next(e for e in state["combatants"] if e["name"] == "Thora")

    before = thora(api.encounter_state(encounter_id))
    api.STATE.sessions.clear()
    after = thora(api.encounter_resume(encounter_id)["state"])

    assert before["stable"] is True and before["dying"] is False
    assert after["stable"] is True, "a recovered fight lost the stabilisation"
    assert after["dying"] is False
    assert after["death_saves"] == before["death_saves"] == {"successes": 3, "failures": 1}


def test_a_facing_survives_the_journal_and_is_still_there_after_recovery() -> None:
    # The test above proves a facing reaches the bundle from a *live* session.
    # This one closes the other door: `normalized_combatant_payload` is the
    # creation input the journal keeps, and `recover_session` rebuilds a fight
    # by feeding it back through `combatants_from_specs`. A key missing from
    # that payload is a key the rebuilt fight has never heard of, however
    # faithfully the live path reports it.
    hero = dict(REPLAY_HERO)
    hero["facing"] = "northeast"
    goblin = dict(REPLAY_GOBLIN)
    goblin["facing"] = "west"
    encounter_id = str(api.encounter_create([hero, goblin], seed=123)["encounter_id"])

    def facings(combatants: list[dict[str, Any]]) -> dict[str, Any]:
        return {entry["name"]: entry.get("facing") for entry in combatants}

    before = facings(api.encounter_state(encounter_id)["combatants"])
    api.STATE.sessions.clear()  # the fight is recovered from its journal alone
    after = facings(api.encounter_resume(encounter_id)["state"]["combatants"])

    assert before == {"Thora": "northeast", "Goblin": "west"}
    assert after == before


def test_a_recovered_fight_exports_a_bundle_that_still_points_its_sight_cones() -> None:
    # The consequence, pinned separately from the mechanism. `renderer.js` skips
    # a seer with no facing — `if (!seer.facing || seer.dead) { continue; }` — so
    # a bundle that lost facing on the way through the journal renders with every
    # sight cone silently absent. `encounter.replay` reaches a fight through
    # `session_for`, which recovers whenever process memory is gone, so this is
    # the ordinary path after any restart and not an exotic one.
    hero = dict(REPLAY_HERO)
    hero["facing"] = "northeast"
    encounter_id = str(
        api.encounter_create([hero, dict(REPLAY_GOBLIN)], seed=124)["encounter_id"]
    )

    api.STATE.sessions.clear()
    bundle = api.replay_export(encounter_id, format_version=2)["bundle"]

    def named(entries: list[dict[str, Any]], name: str) -> dict[str, Any]:
        return next(entry for entry in entries if entry["name"] == name)

    assert named(bundle["latest_state"]["combatants"], "Thora")["facing"] == "northeast"
    assert named(bundle["initial"]["combatants"], "Thora")["facing"] == "northeast"


def test_the_captured_creation_input_carries_facing_even_when_nobody_set_one() -> None:
    # The state payload omits `facing` for an untracked creature; this one does
    # not, and the difference is deliberate — creation input is a fixed set of
    # keys fed back through `combatants_from_specs`, not a report. Pinned
    # because it is otherwise unguarded: making the key conditional here breaks
    # no other test, so nothing would stop the two shapes being quietly merged.
    encounter_id = str(
        api.encounter_create(
            [dict(REPLAY_HERO), dict(REPLAY_GOBLIN)], seed=125
        )["encounter_id"]
    )
    bundle = api.replay_export(encounter_id, format_version=2)["bundle"]

    captured = bundle["initial"]["combatants"]
    assert all("facing" in entry for entry in captured)
    assert [entry["facing"] for entry in captured] == [None, None]
    # ...while the state payload still leaves the key out entirely.
    assert all(
        "facing" not in entry for entry in bundle["latest_state"]["combatants"]
    )


def test_a_facing_nobody_named_is_refused_rather_than_silently_dropped() -> None:
    hero = dict(REPLAY_HERO)
    hero["facing"] = "nrothest"

    with pytest.raises(RequestError, match="facing must be one of the eight directions"):
        api.encounter_create([hero, dict(REPLAY_GOBLIN)], seed=122)


def test_a_fully_populated_attack_option_round_trips_through_the_journal() -> None:
    # Nothing else in the suite pins these three hand-written mirrors of
    # AttackOption together: the dataclass, `_attack_payload`'s dict, and
    # `attack_from_spec`'s reader. `attack_from_spec` reads every key with
    # `.get`, so a field the payload carries but the reader forgets is
    # SILENTLY DROPPED rather than refused, and `test_the_journal_captures_
    # every_key_a_combatant_spec_accepts` above only compares top-level
    # combatant keys — `attacks` passes whatever its contents are.
    #
    # The field set is derived from `dataclasses.fields(AttackOption)`, never
    # hardcoded here, so a field added to the dataclass and forgotten in
    # either mirror is caught two ways: the loop below refuses to let this
    # option's build leave that field at its default (a silent no-op would
    # round-trip trivially), and the round trip itself must reproduce every
    # field the build did set.
    option = AttackOption(
        name="Longbow",
        attack_bonus=6,
        damage=Dice.parse("1d8+3"),
        damage_type=DamageType.PIERCING,
        kind=AttackKind.RANGED,
        reach=10,
        normal_range=150,
        long_range=600,
        bonus_damage=Dice.parse("1d4"),
        bonus_damage_type=DamageType.FIRE,
        advantage_bonus_damage=Dice.parse("1d6"),
        advantage_bonus_with_adjacent_ally=True,
        on_hit_condition="poisoned",
        on_hit_save_ability=Ability.CONSTITUTION,
        on_hit_save_dc=13,
        on_hit_expiry=RiderExpiry.START_OF_ATTACKER_NEXT_TURN,
        on_hit_max_size=Size.LARGE,
        on_hit_attach=True,
        attached_damage=Dice.parse("1d4"),
        attached_damage_type=DamageType.NECROTIC,
        detach_after_damage=10,
        provenance="Original content",
        ammunition="Arrow",
        loading=True,
        thrown=True,
    )
    bare = AttackOption(
        name="x", attack_bonus=0, damage=Dice.parse("1d4"), damage_type=DamageType.SLASHING,
    )
    # Fields whose every legal value is indistinguishable from the bare
    # default above would pass the coverage loop by accident rather than by
    # this test actually exercising them — there are none today, and a field
    # landing here later needs a comment saying why it cannot have one.
    FIELDS_WITH_NO_DISTINCT_VALUE: frozenset[str] = frozenset()
    for attack_field in fields(AttackOption):
        if attack_field.name in FIELDS_WITH_NO_DISTINCT_VALUE:
            continue
        built = getattr(option, attack_field.name)
        default = getattr(bare, attack_field.name)
        assert built != default, (
            f"{attack_field.name} is still at its default in this test's "
            "fully-populated AttackOption — give it a distinct value, or add it "
            "to FIELDS_WITH_NO_DISTINCT_VALUE with a comment saying why it can't"
        )

    payload = replay_service._attack_payload(option)
    round_tripped = specs.attack_from_spec(payload)
    assert round_tripped == option
