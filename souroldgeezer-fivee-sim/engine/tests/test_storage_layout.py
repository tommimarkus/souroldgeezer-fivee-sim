from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from fivee_sim.client import discovery
from fivee_sim.client.cli import EXIT_USAGE
from fivee_sim.client.cli import main as client_main
from fivee_sim.configuration import load_config
from fivee_sim.paths import RunSelectionError, StorageLayout, storage_layout
from fivee_sim.service import adventures, scenes
from fivee_sim.service.errors import RequestError
from fivee_sim.service.sessions import EngineState
from fivee_sim.web import http_server
from fivee_sim.web.http_server import EngineServer


def _configuration(tmp_path: Path):
    config = tmp_path / ".fivee-sim" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        """\
format_version = 1
[storage]
maps = ["shared-maps", "/opt/maps"]
replays = "shared-replays"
scenes = "shared-scenes"
encounters = "legacy-encounters"
adventures = "legacy-adventures"
blobs = "legacy-blobs"
runs = "runs"
""",
        encoding="utf-8",
    )
    return load_config(config)


def _complete_run_at(root: Path, run_id: str) -> None:
    for name in ("maps", "scenes", "replays", "encounters", "adventures", "blobs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "adventures" / "adv-1.json").write_text("{}", encoding="utf-8")


def _complete_manifest_run_at(root: Path, run_id: str) -> None:
    for name in ("maps", "scenes", "replays", "encounters", "adventures", "blobs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "run.json").write_text(
        json.dumps(
            {
                "format": "fivee-sim-run",
                "format_version": 1,
                "id": run_id,
                "created_at": "2026-08-08T00:00:00Z",
                "adventure_id": None,
                "request_ids": {},
            }
        ),
        encoding="utf-8",
    )


def _bound_manifest_run_at(root: Path, run_id: str, members: list[object]) -> None:
    _complete_manifest_run_at(root, run_id)
    adventure_id = "adv-1"
    (root / "run.json").write_text(
        json.dumps(
            {
                "format": "fivee-sim-run",
                "format_version": 1,
                "id": run_id,
                "created_at": "2026-08-08T00:00:00Z",
                "adventure_id": adventure_id,
                "request_ids": {},
            }
        ),
        encoding="utf-8",
    )
    (root / "adventures" / f"{adventure_id}.json").write_text(
        json.dumps(
            {
                "format": adventures.FORMAT,
                "format_version": adventures.FORMAT_VERSION,
                "id": adventure_id,
                "name": "Opening",
                "created_at": "2026-08-08T00:00:00Z",
                "status": "active",
                "members": members,
                "request_ids": {},
            }
        ),
        encoding="utf-8",
    )


def test_a_manifest_run_layout_is_an_immutable_overlay_workspace(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    _complete_manifest_run_at(configuration.runs_dir / "run-7", "run-7")

    layout = storage_layout(configuration=configuration, run_id="run-7")

    assert layout.run_id == "run-7"
    assert layout.run_root == configuration.runs_dir / "run-7"
    assert layout.maps_dir == layout.run_root / "maps"
    assert layout.scenes_dir == layout.run_root / "scenes"
    assert layout.replays_dir == layout.run_root / "replays"
    assert layout.encounters_dir == layout.run_root / "encounters"
    assert layout.adventures_dir == layout.run_root / "adventures"
    assert layout.blobs_dir == layout.run_root / "blobs"
    assert layout.shared_map_paths == configuration.map_paths
    assert layout.shared_replay_paths == configuration.replay_paths
    assert layout.runtime_dir == configuration.path.parent / "runtime" / "run-7"
    with pytest.raises(FrozenInstanceError):
        layout.run_id = "run-8"  # type: ignore[misc]


def test_an_old_adventure_root_is_refused_without_modifying_its_contents(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    legacy_root = configuration.runs_dir / "adv-7"
    _complete_run_at(legacy_root, "adv-7")
    before = (legacy_root / "adventures" / "adv-1.json").read_bytes()

    with pytest.raises(RunSelectionError, match="safe run id"):
        storage_layout(configuration=configuration, run_id="adv-7")

    assert (legacy_root / "adventures" / "adv-1.json").read_bytes() == before


def test_a_bound_run_requires_a_chapter_zero_adventure_document(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    root = configuration.runs_dir / "run-1"
    _bound_manifest_run_at(root, "run-1", [])

    with pytest.raises(RunSelectionError, match="invalid adventure document"):
        storage_layout(configuration=configuration, run_id="run-1")

    _bound_manifest_run_at(
        root,
        "run-1",
        [{"index": 0, "encounter_id": "enc-1", "mode": "combat"}],
    )

    assert storage_layout(configuration=configuration, run_id="run-1").run_root == root


def test_the_adventure_document_parser_refuses_an_empty_persisted_run(tmp_path: Path) -> None:
    path = tmp_path / "adv-1.json"
    document = {
        "format": adventures.FORMAT,
        "format_version": adventures.FORMAT_VERSION,
        "id": "adv-1",
        "name": "Opening",
        "created_at": "2026-08-08T00:00:00Z",
        "status": "active",
        "members": [],
        "request_ids": {},
    }

    with pytest.raises(RequestError, match="chapter-zero member"):
        adventures._parsed(json.dumps(document), path)


def test_start_atomically_binds_a_run_to_a_chapter_zero_adventure(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    scenes.save(
        "opening",
        {
            "name": "opening",
            "combatants": [],
            "seed": 1,
            "mode": "exploration",
            "map": {
                "format": "fivee-sim-map",
                "format_version": 1,
                "name": "ground",
                "grid": {"width": 3, "height": 3, "cell_feet": 5},
                "legend": {".": "floor"},
                "tiles": ["..."] * 3,
                "features": [
                    {"id": "party-arrival", "kind": "spawn", "at": [1, 1], "team": "party"}
                ],
                "provenance": {
                    "generator": "hand", "seed": 1, "params": {}, "edited": False,
                    "source": "test",
                },
            },
        },
        configuration.scenes_dir,
        expected_sha256="*",
    )
    state = EngineState(storage=storage_layout(configuration=configuration))

    started = adventures.start(
        "Opening",
        {
            "scene_id": "opening",
            "party": [{"name": "Thora", "team": "party", "ac": 10, "max_hp": 10}],
        },
        state=state,
    )

    run = configuration.runs_dir / str(started["run_id"])
    manifest = json.loads((run / "run.json").read_text(encoding="utf-8"))
    document = json.loads(
        (run / "adventures" / f"{started['adventure_id']}.json").read_text(encoding="utf-8")
    )
    assert manifest["adventure_id"] == started["adventure_id"]
    assert document["members"][0]["index"] == 0
    assert document["members"][0]["encounter_id"] == started["encounter_id"]


def test_control_and_legacy_have_separate_rendezvous_and_legacy_roots(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)

    control = storage_layout(configuration=configuration)
    legacy = storage_layout(configuration=configuration, run_id="legacy")

    assert control.runtime_dir == configuration.path.parent / "runtime" / "control"
    assert control.run_root is None
    assert legacy.runtime_dir == configuration.path.parent / "runtime" / "legacy"
    assert legacy.encounters_dir == configuration.encounters_dir
    assert legacy.adventures_dir == configuration.adventures_dir
    assert legacy.blobs_dir == configuration.blobs_dir


@pytest.mark.parametrize("run_id", ["adv-missing", "../adv-1", "", "adv-", "run-1"])
def test_an_unknown_or_unsafe_run_is_refused(tmp_path: Path, run_id: str) -> None:
    configuration = _configuration(tmp_path)

    with pytest.raises(RunSelectionError, match="run"):
        storage_layout(configuration=configuration, run_id=run_id)


def test_a_symlinked_run_root_is_refused(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    outside = tmp_path / "outside" / "run-1"
    _complete_manifest_run_at(outside, "run-1")
    configuration.runs_dir.mkdir(parents=True)
    (configuration.runs_dir / "run-1").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RunSelectionError, match="symbolic link"):
        storage_layout(configuration=configuration, run_id="run-1")


def test_a_symlinked_mutable_run_directory_is_refused(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    _complete_manifest_run_at(configuration.runs_dir / "run-1", "run-1")
    maps = configuration.runs_dir / "run-1" / "maps"
    maps.rmdir()
    outside = tmp_path / "outside-maps"
    outside.mkdir()
    maps.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RunSelectionError, match="symbolic link"):
        storage_layout(configuration=configuration, run_id="run-1")


def test_storage_layout_can_be_constructed_directly_only_with_complete_roots(
    tmp_path: Path,
) -> None:
    layout = StorageLayout(
        run_id=None,
        runs_dir=tmp_path / "runs",
        runtime_dir=tmp_path / "runtime" / "control",
        shared_map_paths=(tmp_path / "maps",),
        shared_replay_paths=(tmp_path / "replays",),
        shared_scenes_dir=tmp_path / "scenes",
        legacy_encounters_dir=tmp_path / "encounters",
        legacy_adventures_dir=tmp_path / "adventures",
        legacy_blobs_dir=tmp_path / "blobs",
    )

    assert layout.maps_dir == tmp_path / "maps"


def test_the_server_composition_root_owns_the_selected_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _configuration(tmp_path)
    _complete_manifest_run_at(configuration.runs_dir / "run-7", "run-7")

    class BoundServer:
        server_address = ("127.0.0.1", 4312)

        def __init__(self, address: object, handler: object) -> None:
            self.engine = None

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(http_server, "_EngineHTTPServer", BoundServer)

    server = EngineServer(configuration=configuration, run_id="run-7")
    try:
        assert server.storage.run_id == "run-7"
        assert server.maps_dir == configuration.runs_dir / "run-7" / "maps"
        assert server.replays_dir == configuration.runs_dir / "run-7" / "replays"
        assert server.state.storage is server.storage
    finally:
        server.close()


def test_discovery_uses_a_separate_state_file_for_every_selector(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    _complete_manifest_run_at(configuration.runs_dir / "run-7", "run-7")

    assert discovery.state_path_for(configuration=configuration) == (
        configuration.path.parent / "runtime" / "control" / "fivee-sim-server.json"
    )
    assert discovery.state_path_for(configuration=configuration, run_id="legacy") == (
        configuration.path.parent / "runtime" / "legacy" / "fivee-sim-server.json"
    )
    assert discovery.state_path_for(configuration=configuration, run_id="run-7") == (
        configuration.path.parent / "runtime" / "run-7" / "fivee-sim-server.json"
    )


def test_the_client_refuses_an_unknown_global_run_before_starting_a_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configuration = _configuration(tmp_path)

    result = client_main(
        ["--run", "adv-missing", "help"],
        configuration=configuration,
        configuration_resolved=True,
    )

    assert result == EXIT_USAGE
    assert "run 'adv-missing' is not a safe run id" in capsys.readouterr().err


def test_discovery_reports_run_identity_from_the_live_ping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        discovery,
        "read_state",
        lambda path: {"port": 4312, "token": "token", "run_id": "stale"},
    )
    monkeypatch.setattr(
        discovery,
        "ping",
        lambda port, token: {
            "run_id": "adv-7",
            "run_root": str(tmp_path / "runs" / "adv-7"),
            "runtime_dir": str(tmp_path / "runtime" / "adv-7"),
        },
    )

    server = discovery.find_running(tmp_path / "state.json")

    assert server is not None
    assert server.run_id == "adv-7"
    assert server.run_root == str(tmp_path / "runs" / "adv-7")
    assert server.runtime_dir == str(tmp_path / "runtime" / "adv-7")
