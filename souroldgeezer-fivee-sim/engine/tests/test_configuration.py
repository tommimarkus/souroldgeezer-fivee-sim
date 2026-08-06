from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from fivee_sim.configuration import (
    CONFIG_SUBPATH,
    ConfigurationError,
    apply_to_environment,
    extract_config_argument,
    find_and_load_config,
    find_config,
    load_config,
)


def _write_config(project: Path, text: str) -> Path:
    path = project / CONFIG_SUBPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_find_config_searches_the_start_directory_and_its_parents(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "format_version = 1\n")
    nested = tmp_path / "one" / "two"
    nested.mkdir(parents=True)

    assert find_config(nested) == config_path.resolve()
    assert find_config(tmp_path.parent / "absent") is None


def test_load_config_parses_and_resolves_every_setting(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
format_version = 1

[content]
paths = ["packs", "/opt/shared/packs"]
builtin = "exclude"

[storage]
maps = ["maps", "/opt/shared/maps"]
replays = "replays"
scenes = "scenes"
encounters = "/opt/shared/encounters"

[development]
reload = true
""",
    )

    config = load_config(config_path)

    assert config.path == config_path.resolve()
    assert config.project_dir == tmp_path.resolve()
    assert config.content_paths == (
        (config_path.parent / "packs").resolve(),
        Path("/opt/shared/packs"),
    )
    assert config.builtin == "exclude"
    assert config.map_paths == (
        (config_path.parent / "maps").resolve(),
        Path("/opt/shared/maps"),
    )
    assert config.replay_paths == ((config_path.parent / "replays").resolve(),)
    assert config.scenes_dir == (config_path.parent / "scenes").resolve()
    assert config.encounters_dir == Path("/opt/shared/encounters")
    assert config.reload is True

    with pytest.raises(FrozenInstanceError):
        config.reload = False  # type: ignore[misc]


def test_load_config_uses_defaults_beside_the_config_file(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "format_version = 1\n")
    config_dir = config_path.parent.resolve()

    without_content = load_config(config_path)
    assert without_content.content_paths == ()
    assert without_content.builtin == "include"
    assert without_content.map_paths == (config_dir / "maps",)
    assert without_content.replay_paths == (config_dir / "replays",)
    assert without_content.scenes_dir == config_dir / "scenes"
    assert without_content.encounters_dir == config_dir / "encounters"
    assert without_content.reload is False

    (config_path.parent / "content").mkdir()
    with_content = load_config(config_path)
    assert with_content.content_paths == (config_dir / "content",)


@pytest.mark.parametrize(
    ("document", "key"),
    [
        ("format_version = 2\n", "format_version"),
        ("format_version = true\n", "format_version"),
        ("format_version = 1\nunknown = true\n", "unknown"),
        ("format_version = 1\n[unknown]\nvalue = true\n", "unknown"),
        ("format_version = 1\ncontent = []\n", "content"),
        ("format_version = 1\n[content]\nunknown = true\n", "content.unknown"),
        ("format_version = 1\n[content]\npaths = [1]\n", "content.paths"),
        ("format_version = 1\n[content]\npaths = [\"\"]\n", "content.paths"),
        ("format_version = 1\n[content]\nbuiltin = \"sometimes\"\n", "content.builtin"),
        ("format_version = 1\n[storage]\nmaps = 1\n", "storage.maps"),
        ("format_version = 1\n[storage]\nreplays = [\"ok\", 1]\n", "storage.replays"),
        ("format_version = 1\n[storage]\nscenes = []\n", "storage.scenes"),
        ("format_version = 1\n[storage]\nencounters = false\n", "storage.encounters"),
        ("format_version = 1\n[development]\nreload = 1\n", "development.reload"),
    ],
)
def test_load_config_rejects_unknown_or_invalid_values(
    tmp_path: Path, document: str, key: str
) -> None:
    config_path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError) as caught:
        load_config(config_path)

    message = str(caught.value)
    assert str(config_path.resolve()) in message
    assert key in message


def test_load_config_rejects_invalid_toml_and_non_files(tmp_path: Path) -> None:
    invalid = _write_config(tmp_path, "format_version = [\n")
    with pytest.raises(ConfigurationError, match="invalid TOML"):
        load_config(invalid)

    with pytest.raises(ConfigurationError, match="regular file"):
        load_config(tmp_path / "missing.toml")
    with pytest.raises(ConfigurationError, match="regular file"):
        load_config(tmp_path)


def test_find_and_load_config_honours_explicit_precedence(tmp_path: Path) -> None:
    discovered = _write_config(tmp_path, "format_version = 1\n")
    explicit = tmp_path / "explicit.toml"
    explicit.write_text(
        'format_version = 1\n[content]\nbuiltin = "exclude"\n',
        encoding="utf-8",
    )

    loaded = find_and_load_config(tmp_path, explicit=explicit)
    assert loaded is not None
    assert loaded.path == explicit.resolve()
    assert loaded.builtin == "exclude"

    discovered.unlink()
    assert find_and_load_config(tmp_path) is None
    with pytest.raises(ConfigurationError, match="regular file"):
        find_and_load_config(tmp_path, explicit=tmp_path / "missing.toml")


@pytest.mark.parametrize(
    ("tokens", "expected_path", "expected_remaining"),
    [
        (
            ["map.list", "--config", "project.toml", "--json"],
            Path("project.toml"),
            ["map.list", "--json"],
        ),
        (["--config=.fivee-sim/config.toml", "help"], Path(".fivee-sim/config.toml"), ["help"]),
        (["help"], None, ["help"]),
    ],
)
def test_extract_config_argument(
    tokens: list[str], expected_path: Path | None, expected_remaining: list[str]
) -> None:
    assert extract_config_argument(tokens) == (expected_path, expected_remaining)


@pytest.mark.parametrize(
    "tokens",
    [
        ["--config"],
        ["--config", ""],
        ["--config=   "],
        ["--config", "one.toml", "--config=two.toml"],
    ],
)
def test_extract_config_argument_rejects_missing_blank_and_duplicate_values(
    tokens: list[str],
) -> None:
    with pytest.raises(ConfigurationError, match="--config"):
        extract_config_argument(tokens)


def test_apply_to_environment_replaces_all_legacy_user_settings(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
format_version = 1
[content]
paths = []
builtin = "exclude"
[storage]
maps = ["maps-a", "maps-b"]
replays = ["replays-a", "replays-b"]
scenes = "scenes"
encounters = "encounters"
[development]
reload = true
""",
    )
    config = load_config(config_path)
    legacy_names = {
        "FIVEE_SIM_PROJECT_DIR",
        "FIVEE_SIM_CONTENT",
        "FIVEE_SIM_BUILTIN",
        "FIVEE_SIM_MAPS",
        "FIVEE_SIM_REPLAYS",
        "FIVEE_SIM_SCENES",
        "FIVEE_SIM_ENCOUNTERS",
        "FIVEE_SIM_RELOAD",
    }
    environment = dict.fromkeys(legacy_names, "stale")
    environment["KEEP_ME"] = "yes"

    apply_to_environment(config, environment)

    assert environment == {
        "KEEP_ME": "yes",
        "FIVEE_SIM_PROJECT_DIR": str(tmp_path.resolve()),
        "FIVEE_SIM_BUILTIN": "exclude",
        "FIVEE_SIM_MAPS": os.pathsep.join(str(path) for path in config.map_paths),
        "FIVEE_SIM_REPLAYS": os.pathsep.join(str(path) for path in config.replay_paths),
        "FIVEE_SIM_SCENES": str(config.scenes_dir),
        "FIVEE_SIM_ENCOUNTERS": str(config.encounters_dir),
        "FIVEE_SIM_RELOAD": "1",
    }


def test_apply_to_environment_leaves_reload_unset_when_disabled(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, "format_version = 1\n"))
    environment = {"FIVEE_SIM_RELOAD": "1"}

    apply_to_environment(config, environment)

    assert "FIVEE_SIM_RELOAD" not in environment
