from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fivee_sim.paths import StorageLayout
from fivee_sim.service import adventures, maps, scenes, sessions
from fivee_sim.service.errors import IdempotencyConflictError, RequestError

from . import api
from .conftest import FIXTURE


def _storage(tmp_path: Path, run_id: str | None = None) -> StorageLayout:
    return StorageLayout(
        run_id=run_id,
        runs_dir=tmp_path / "runs",
        runtime_dir=tmp_path / "runtime" / (run_id or "control"),
        shared_map_paths=(tmp_path / "maps",),
        shared_replay_paths=(tmp_path / "replays",),
        shared_scenes_dir=tmp_path / "scenes",
        legacy_encounters_dir=tmp_path / "legacy-encounters",
        legacy_adventures_dir=tmp_path / "legacy-adventures",
        legacy_blobs_dir=tmp_path / "legacy-blobs",
    )


def _map() -> dict[str, Any]:
    return {
        "format": "fivee-sim-map",
        "format_version": 1,
        "name": "Opening ground",
        "grid": {"width": 4, "height": 4, "cell_feet": 5},
        "legend": {".": "normal"},
        "tiles": ["...."] * 4,
        "features": [
            {"id": "party-one", "kind": "spawn", "at": [0, 0], "team": "party"},
            {"id": "party-two", "kind": "spawn", "at": [1, 0], "team": "party"},
            {"id": "party-three", "kind": "spawn", "at": [2, 0], "team": "party"},
        ],
        "provenance": {
            "generator": "hand",
            "seed": 0,
            "params": {},
            "edited": False,
            "source": FIXTURE,
        },
    }


def _scene() -> dict[str, Any]:
    return {
        "name": "Opening scene",
        "mode": "exploration",
        "seed": 7,
        "movement_rule": "5-5-5",
        "map": _map(),
        "combatants": [
            {"name": "First placeholder", "team": "party", "ac": 10, "max_hp": 8},
            {"name": "Second placeholder", "team": "party", "ac": 10, "max_hp": 8},
            {
                "name": "Mill rat",
                "team": "monsters",
                "ac": 10,
                "max_hp": 6,
                "position": [17, 17],
                "attacks": [],
            },
        ],
    }


def _party() -> list[dict[str, Any]]:
    return [
        {"name": "Kettle", "team": "party", "ac": 12, "max_hp": 11},
        {"name": "Bo", "team": "party", "ac": 13, "max_hp": 10},
    ]


def test_start_publishes_one_bound_run_and_exploration_opening(
    tmp_path: Path, monkeypatch: Any
) -> None:
    control = _storage(tmp_path)
    monkeypatch.setattr(api.STATE, "storage", control)
    scenes.save("opening", _scene(), root=control.shared_scenes_dir)

    opened = adventures.start(
        "The Drowned Mill",
        {"scene_id": "opening", "party": _party(), "seed": 19},
        request_id="open-mill",
        state=api.STATE,
    )

    assert opened["run_id"] == "run-1"
    assert opened["adventure_id"] == "adv-1"
    assert opened["chapter_index"] == 0
    assert opened["encounter"]["state"]["mode"] == "exploration"
    assert opened["encounter"]["seed"] == 19
    roster = opened["encounter"]["state"]["combatants"]
    assert {one["name"]: (one["position"], one.get("level")) for one in roster} == {
        "Kettle": ([0, 0], 0),
        "Bo": ([5, 0], 0),
        "Mill rat": ([15, 15], 0),
    }
    assert opened["run"]["adventure_id"] == "adv-1"
    assert opened["adventure"]["members"] == [
        {
            "index": 0,
            "encounter_id": opened["encounter_id"],
            "mode": "exploration",
            "carried": [],
            "linked_at": opened["adventure"]["members"][0]["linked_at"],
        }
    ]
    root = control.runs_dir / "run-1"
    assert (root / "run.json").is_file()
    assert (root / "adventures" / "adv-1.json").is_file()
    assert (root / "encounters" / opened["encounter_id"] / "journal.jsonl").is_file()
    retried = adventures.start(
        "The Drowned Mill",
        {"scene_id": "opening", "party": _party(), "seed": 19},
        request_id="open-mill",
        state=api.STATE,
    )
    assert (retried["run_id"], retried["adventure_id"], retried["encounter_id"]) == (
        opened["run_id"],
        opened["adventure_id"],
        opened["encounter_id"],
    )


def test_start_skips_a_party_spawn_occupied_by_preserved_cast(
    tmp_path: Path, monkeypatch: Any
) -> None:
    control = _storage(tmp_path)
    monkeypatch.setattr(api.STATE, "storage", control)
    scene = _scene()
    scene["combatants"][-1]["position"] = [0, 0]
    scenes.save("occupied", scene, root=control.shared_scenes_dir)

    opened = adventures.start(
        "Occupied opening", {"scene_id": "occupied", "party": _party()}, state=api.STATE
    )

    roster = {one["name"]: one["position"] for one in opened["encounter"]["state"]["combatants"]}
    assert roster["Kettle"] == [5, 0]
    assert roster["Bo"] == [10, 0]


@pytest.mark.parametrize(
    ("opening", "message"),
    [
        ({"scene_id": "no-map", "party": _party()}, "requires a map"),
        ({"scene_id": "valid", "party": [{"name": "Nope", "team": "monsters"}]}, "team 'party'"),
    ],
)
def test_start_refusals_publish_nothing(
    tmp_path: Path, monkeypatch: Any, opening: dict[str, Any], message: str
) -> None:
    control = _storage(tmp_path)
    monkeypatch.setattr(api.STATE, "storage", control)
    scenes.save(
        "no-map",
        {key: value for key, value in _scene().items() if key != "map"},
        root=control.shared_scenes_dir,
    )
    scenes.save("valid", _scene(), root=control.shared_scenes_dir)

    with pytest.raises(RequestError, match=message):
        adventures.start("Refused", opening, state=api.STATE)

    assert not control.runs_dir.exists() or not list(control.runs_dir.glob("run-*"))
    assert not api.STATE.sessions


def test_start_rolls_back_encounter_when_adventure_write_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    control = _storage(tmp_path)
    monkeypatch.setattr(api.STATE, "storage", control)
    scenes.save("opening", _scene(), root=control.shared_scenes_dir)
    monkeypatch.setattr(
        adventures,
        "_render",
        lambda *args, **kwargs: (_ for _ in ()).throw(RequestError("write failed")),
    )

    with pytest.raises(RequestError, match="write failed"):
        adventures.start("Rollback", {"scene_id": "opening", "party": _party()}, state=api.STATE)

    assert not list(control.runs_dir.glob("run-*"))
    assert not api.STATE.sessions


@pytest.mark.parametrize(
    ("features", "message"),
    [
        ([{"id": "only", "kind": "spawn", "at": [0, 0], "team": "party"}], "insufficient"),
        (
            [
                {"id": "one", "kind": "spawn", "at": [0, 0], "team": "party"},
                {"id": "two", "kind": "spawn", "at": [0, 0], "team": "party"},
            ],
            "duplicate",
        ),
    ],
)
def test_start_spawn_refusals_publish_nothing(
    tmp_path: Path, monkeypatch: Any, features: list[dict[str, Any]], message: str
) -> None:
    control = _storage(tmp_path)
    monkeypatch.setattr(api.STATE, "storage", control)
    scene = _scene()
    scene["map"]["features"] = features
    scenes.save("bad-spawns", scene, root=control.shared_scenes_dir)

    with pytest.raises(RequestError, match=message):
        adventures.start(
            "Bad spawns", {"scene_id": "bad-spawns", "party": _party()}, state=api.STATE
        )

    assert not list(control.runs_dir.glob("run-*"))
    assert not api.STATE.sessions


def test_start_reused_key_with_changed_identity_conflicts(tmp_path: Path, monkeypatch: Any) -> None:
    control = _storage(tmp_path)
    monkeypatch.setattr(api.STATE, "storage", control)
    scenes.save("opening", _scene(), root=control.shared_scenes_dir)
    adventures.start(
        "Same", {"scene_id": "opening", "party": _party()}, request_id="once", state=api.STATE
    )

    with pytest.raises(IdempotencyConflictError, match="idempotency key 'once'"):
        adventures.start(
            "Changed",
            {"scene_id": "opening", "party": _party()},
            request_id="once",
            state=api.STATE,
        )


def test_start_preserves_saved_map_reference_and_scene_defaults(
    tmp_path: Path, monkeypatch: Any
) -> None:
    control = _storage(tmp_path)
    monkeypatch.setattr(api.STATE, "storage", control)
    terrain = sessions.active_content(api.STATE).registry.terrain_effects
    document, _warnings = maps.parse_payload(_map(), source="opening-map", terrain=terrain)
    maps.save_file(document, control.shared_map_paths[0] / "opening-map.json")
    scene = _scene()
    scene.pop("map")
    scene["map_id"] = "opening-map"
    scenes.save("referenced", scene, root=control.shared_scenes_dir)

    opened = adventures.start(
        "Referenced", {"scene_id": "referenced", "party": _party()}, state=api.STATE
    )

    assert opened["encounter"]["seed"] == 7
    assert opened["encounter"]["state"]["movement_rule"] == "5-5-5"
    assert opened["encounter"]["map_source"]["map_id"] == "opening-map"


def test_start_assigns_ground_then_stored_level_order(
    tmp_path: Path, monkeypatch: Any
) -> None:
    control = _storage(tmp_path)
    monkeypatch.setattr(api.STATE, "storage", control)
    scene = _scene()
    scene["combatants"] = [
        {"name": "Ground placeholder", "team": "party", "ac": 10, "max_hp": 8}
    ]
    scene["map"]["features"] = [
        {"id": "ground", "kind": "spawn", "at": [0, 0], "team": "party"}
    ]
    scene["map"]["levels"] = [
        {
            "index": 2,
            "name": "second stored",
            "tiles": ["...."] * 4,
            "elevation": {"default": 0, "squares": []},
            "features": [{"id": "two", "kind": "spawn", "at": [1, 0], "team": "party"}],
        },
        {
            "index": 1,
            "name": "first stored",
            "tiles": ["...."] * 4,
            "elevation": {"default": 0, "squares": []},
            "features": [{"id": "one", "kind": "spawn", "at": [2, 0], "team": "party"}],
        },
    ]
    scenes.save("ordered", scene, root=control.shared_scenes_dir)
    party = [
        {"name": "Ground", "team": "party", "ac": 10, "max_hp": 8},
        {"name": "Second", "team": "party", "ac": 10, "max_hp": 8},
        {"name": "First", "team": "party", "ac": 10, "max_hp": 8},
    ]

    opened = adventures.start("Ordered", {"scene_id": "ordered", "party": party}, state=api.STATE)

    roster = {entry["name"]: (entry["position"], entry["level"])
              for entry in opened["encounter"]["state"]["combatants"]}
    assert roster == {"Ground": ([0, 0], 0), "Second": ([5, 0], 2), "First": ([10, 0], 1)}
