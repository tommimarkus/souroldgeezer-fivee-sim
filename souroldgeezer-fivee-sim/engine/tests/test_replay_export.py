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
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.mcp_server import server as api
from fivee_sim.service import replay as replay_service

FIXTURE = "Authored for the test suite; 5E-compatible original content"

HERO: dict[str, Any] = {
    "name": "Thora",
    "team": "party",
    "ac": 16,
    "max_hp": 30,
    "position": [5, 5],
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
GOBLIN: dict[str, Any] = {
    "monster": "Goblin Warrior",
    "label": "Goblin",
    "team": "monsters",
    "position": [15, 15],
}


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


def mapless_fight(seed: int = 41) -> str:
    created = api.encounter_create([dict(HERO), dict(GOBLIN)], seed=seed)
    return str(created["encounter_id"])


def mapped_fight(seed: int = 43) -> tuple[str, str]:
    map_id = str(api.map_load(document=chamber())["map_id"])
    created = api.encounter_create([dict(HERO), dict(GOBLIN)], seed=seed, map_id=map_id)
    return str(created["encounter_id"]), map_id


class TestBundleSchema:
    def test_the_bundle_carries_exactly_the_viewer_contract(self) -> None:
        encounter_id = mapless_fight()
        result = api.replay_export(encounter_id)
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
        bundle = api.replay_export(encounter_id)["bundle"]
        starts = {c["name"]: c["position"] for c in bundle["initial"]["creatures"]}
        assert starts[mover] in ([5, 5], [15, 15])
        moves = [e for e in bundle["events"] if e["kind"] == "move"]
        assert moves, "the move must be in the exported log"
        # In-process the payload holds a tuple; over the wire it is a JSON list.
        assert list(moves[-1]["data"]["destination"]) == [30, 25]

    def test_the_events_are_the_whole_log_so_far(self) -> None:
        encounter_id = mapless_fight()
        api.encounter_advance(encounter_id)
        bundle = api.replay_export(encounter_id)["bundle"]
        log = api.encounter_log(encounter_id, include_actions=False)
        assert len(bundle["events"]) == log["total_events"]
        assert bundle["events"][0]["kind"] == "round"

    def test_an_unknown_encounter_is_refused(self) -> None:
        with pytest.raises(api.ToolError, match="unknown encounter"):
            api.replay_export("enc-never")


class TestSizeGate:
    def test_a_small_bundle_comes_back_inline(self) -> None:
        result = api.replay_export(mapless_fight())
        assert "bundle" in result
        assert "path" not in result
        assert result["bytes"] <= api._INLINE_BUNDLE_BYTES

    def test_a_large_bundle_goes_to_disk_at_the_default_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIVEE_SIM_MAPS", str(tmp_path))
        monkeypatch.setattr(api, "_INLINE_BUNDLE_BYTES", 64)
        encounter_id = mapless_fight(seed=47)
        result = api.replay_export(encounter_id)
        assert "bundle" not in result
        assert result["path"] == str(
            tmp_path / "replays" / f"{encounter_id}-{result['seed']}.json"
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
        bundle = api.replay_export(encounter_id)["bundle"]
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
        bundle = api.replay_export(encounter_id)["bundle"]
        assert bundle["map"]["name"] == "replay chamber"
        assert bundle["map"]["tiles"] == chamber()["tiles"]
        assert bundle["map"]["provenance"]["edited"] is False

    def test_an_inline_map_fight_replays_on_the_neutral_plane(self) -> None:
        created = api.encounter_create(
            [dict(HERO), dict(GOBLIN)],
            seed=61,
            map={
                "width": 6, "height": 5,
                "rows": ["......", "......", "......", "......", "......"],
                "legend": {".": "normal"},
            },
        )
        bundle = api.replay_export(str(created["encounter_id"]))["bundle"]
        assert bundle["map"] is None

    def test_a_mapless_fight_carries_no_map(self) -> None:
        bundle = api.replay_export(mapless_fight())["bundle"]
        assert bundle["map"] is None
        assert bundle["initial"]["map_open_features"] == []
