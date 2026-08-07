from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from fivee_sim.client import discovery
from fivee_sim.client.cli import EXIT_USAGE
from fivee_sim.client.cli import main as client_main
from fivee_sim.configuration import Configuration, load_config
from fivee_sim.paths import RunSelectionError, StorageLayout, storage_layout
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


def _complete_run(configuration: Configuration, run_id: str) -> None:
    root = configuration.runs_dir / run_id
    _complete_run_at(root, run_id)


def _complete_run_at(root: Path, run_id: str) -> None:
    for name in ("maps", "scenes", "replays", "encounters", "adventures", "blobs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "adventures" / f"{run_id}.json").write_text("{}", encoding="utf-8")


def test_an_adventure_run_layout_is_an_immutable_overlay_workspace(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    _complete_run(configuration, "adv-7")

    layout = storage_layout(configuration=configuration, run_id="adv-7")

    assert layout.run_id == "adv-7"
    assert layout.run_root == configuration.runs_dir / "adv-7"
    assert layout.maps_dir == layout.run_root / "maps"
    assert layout.scenes_dir == layout.run_root / "scenes"
    assert layout.replays_dir == layout.run_root / "replays"
    assert layout.encounters_dir == layout.run_root / "encounters"
    assert layout.adventures_dir == layout.run_root / "adventures"
    assert layout.blobs_dir == layout.run_root / "blobs"
    assert layout.shared_map_paths == configuration.map_paths
    assert layout.shared_replay_paths == configuration.replay_paths
    assert layout.runtime_dir == configuration.path.parent / "runtime" / "adv-7"
    with pytest.raises(FrozenInstanceError):
        layout.run_id = "adv-8"  # type: ignore[misc]


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
    outside = tmp_path / "outside" / "adv-linked"
    _complete_run_at(outside, "adv-linked")
    configuration.runs_dir.mkdir(parents=True)
    (configuration.runs_dir / "adv-linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RunSelectionError, match="symbolic link"):
        storage_layout(configuration=configuration, run_id="adv-linked")


def test_a_symlinked_mutable_run_directory_is_refused(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    _complete_run(configuration, "adv-linked-child")
    maps = configuration.runs_dir / "adv-linked-child" / "maps"
    maps.rmdir()
    outside = tmp_path / "outside-maps"
    outside.mkdir()
    maps.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RunSelectionError, match="symbolic link"):
        storage_layout(configuration=configuration, run_id="adv-linked-child")


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
    _complete_run(configuration, "adv-7")

    class BoundServer:
        server_address = ("127.0.0.1", 4312)

        def __init__(self, address: object, handler: object) -> None:
            self.engine = None

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(http_server, "_EngineHTTPServer", BoundServer)

    server = EngineServer(configuration=configuration, run_id="adv-7")
    try:
        assert server.storage.run_id == "adv-7"
        assert server.maps_dir == configuration.runs_dir / "adv-7" / "maps"
        assert server.replays_dir == configuration.runs_dir / "adv-7" / "replays"
        assert server.state.storage is server.storage
    finally:
        server.close()


def test_discovery_uses_a_separate_state_file_for_every_selector(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    _complete_run(configuration, "adv-7")

    assert discovery.state_path_for(configuration=configuration) == (
        configuration.path.parent / "runtime" / "control" / "fivee-sim-server.json"
    )
    assert discovery.state_path_for(configuration=configuration, run_id="legacy") == (
        configuration.path.parent / "runtime" / "legacy" / "fivee-sim-server.json"
    )
    assert discovery.state_path_for(configuration=configuration, run_id="adv-7") == (
        configuration.path.parent / "runtime" / "adv-7" / "fivee-sim-server.json"
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
    assert "run 'adv-missing' does not exist" in capsys.readouterr().err


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
