"""Scene-backed adventure factories for service-level tests.

Persisted adventures are born with chapter zero.  Tests that need a run therefore
start the same compound operation as callers and then select the published run
before exercising run-scoped services.
"""

from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from fivee_sim.paths import StorageLayout
from fivee_sim.service import adventures, scenes

from . import api
from .conftest import FIXTURE


def _opening_map(combatants: list[dict[str, Any]]) -> dict[str, Any]:
    party = [entry for entry in combatants if entry.get("team") == "party"]
    features = []
    for index, entry in enumerate(party):
        position = entry.get("position", [index * 5, 0])
        if isinstance(position, int):
            at = [position % 64, position // 64]
        else:
            at = [int(position[0]) // 5, int(position[1]) // 5]
        features.append(
            {"id": f"party-{index + 1}", "kind": "spawn", "at": at, "team": "party"}
        )
    return {
        "format": "fivee-sim-map",
        "format_version": 1,
        "name": "Test opening",
        "grid": {"width": 64, "height": 64, "cell_feet": 5},
        "legend": {".": "normal"},
        "tiles": ["." * 64 for _ in range(64)],
        "features": features,
        "provenance": {
            "generator": "hand",
            "seed": 0,
            "params": {},
            "edited": False,
            "source": FIXTURE,
        },
    }


def start_adventure(
    name: str,
    *,
    combatants: list[dict[str, Any]] | None = None,
    seed: int | None = None,
    movement_rule: str = "5-5-5",
    request_id: str | None = None,
    mode: str = "combat",
    map: dict[str, Any] | None = None,
    map_id: str | None = None,
) -> dict[str, Any]:
    """Publish chapter zero, select its run, and return the compound response."""
    if api.STATE.storage is None:
        root = Path(os.environ["FIVEE_SIM_RUNS"])
        api.STATE.storage = StorageLayout(
            run_id=None,
            runs_dir=root,
            runtime_dir=root.parent / "runtime" / "control",
            shared_map_paths=(Path(os.environ["FIVEE_SIM_MAPS"]),),
            shared_replay_paths=(Path(os.environ["FIVEE_SIM_REPLAYS"]),),
            shared_scenes_dir=Path(os.environ["FIVEE_SIM_SCENES"]),
            legacy_encounters_dir=Path(os.environ["FIVEE_SIM_ENCOUNTERS"]),
            legacy_adventures_dir=Path(os.environ["FIVEE_SIM_ADVENTURES"]),
            legacy_blobs_dir=Path(os.environ["FIVEE_SIM_BLOBS"]),
        )
    elif api.STATE.storage.run_id is not None:
        api.STATE.storage = replace(api.STATE.storage, run_id=None)
    scene_id = (
        f"test-opening-{request_id}"
        if request_id is not None
        else f"test-opening-{len(list(api.STATE.storage.runs_dir.glob('run-*'))) + 1}"
    )
    supplied = combatants or [
        {"name": "Opening hero", "team": "party", "ac": 10, "max_hp": 10},
        {
            "name": "Opening foe",
            "team": "monsters",
            "ac": 10,
            "max_hp": 10,
            "position": [5, 0],
        },
    ]
    roster = [deepcopy(entry) for entry in supplied]
    party = [entry for entry in roster if entry.get("team") == "party"]
    cast = [entry for entry in roster if entry.get("team") != "party"]
    scene: dict[str, Any] = {
        "name": "Test opening",
        "mode": mode,
        "seed": seed,
        "movement_rule": movement_rule,
        "combatants": cast,
    }
    if map_id is not None:
        scene["map_id"] = map_id
    else:
        scene["map"] = deepcopy(map) if map is not None else _opening_map(roster)
    scenes.save(
        scene_id,
        scene,
        root=api.STATE.storage.shared_scenes_dir,
        expected_sha256="*",
    )
    opened = adventures.start(
        name,
        {"scene_id": scene_id, "party": party, "seed": seed},
        request_id=request_id,
        state=api.STATE,
    )
    api.STATE.storage = replace(api.STATE.storage, run_id=str(opened["run_id"]))
    return {
        **opened,
        "id": opened["adventure_id"],
        "index": 0,
        "carried": [],
    }
