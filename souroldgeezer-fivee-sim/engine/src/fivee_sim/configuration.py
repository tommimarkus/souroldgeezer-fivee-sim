"""Strict project-file configuration and its process compatibility adapter."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CONFIG_SUBPATH = Path(".fivee-sim/config.toml")

_TOP_LEVEL_KEYS = frozenset({"format_version", "content", "storage", "development"})
_CONTENT_KEYS = frozenset({"paths", "builtin"})
_STORAGE_KEYS = frozenset({"maps", "replays", "scenes", "encounters"})
_DEVELOPMENT_KEYS = frozenset({"reload"})

_PROJECT_ENV = "FIVEE_SIM_PROJECT_DIR"
_CONTENT_ENV = "FIVEE_SIM_CONTENT"
_BUILTIN_ENV = "FIVEE_SIM_BUILTIN"
_MAPS_ENV = "FIVEE_SIM_MAPS"
_REPLAYS_ENV = "FIVEE_SIM_REPLAYS"
_SCENES_ENV = "FIVEE_SIM_SCENES"
_ENCOUNTERS_ENV = "FIVEE_SIM_ENCOUNTERS"
_RELOAD_ENV = "FIVEE_SIM_RELOAD"
LEGACY_PROJECT_ENVIRONMENT = (
    _PROJECT_ENV,
    _CONTENT_ENV,
    _BUILTIN_ENV,
    _MAPS_ENV,
    _REPLAYS_ENV,
    _SCENES_ENV,
    _ENCOUNTERS_ENV,
    _RELOAD_ENV,
)

BuiltinMode = Literal["include", "exclude"]


class ConfigurationError(ValueError):
    """A project configuration file or ``--config`` argument is invalid."""


@dataclass(frozen=True, slots=True)
class Configuration:
    """One completely parsed project configuration.

    Every path is absolute. The value has no ambient environment dependency;
    :func:`apply_to_environment` is the explicit compatibility seam for existing
    process consumers.
    """

    path: Path
    project_dir: Path
    content_paths: tuple[Path, ...]
    builtin: BuiltinMode
    map_paths: tuple[Path, ...]
    replay_paths: tuple[Path, ...]
    scenes_dir: Path
    encounters_dir: Path
    reload: bool


def find_config(start: str | os.PathLike[str]) -> Path | None:
    """Find the nearest regular ``.fivee-sim/config.toml`` at or above *start*."""

    directory = Path(start).resolve()
    if directory.is_file():
        directory = directory.parent
    for candidate_dir in (directory, *directory.parents):
        candidate = candidate_dir / CONFIG_SUBPATH
        if candidate.is_file():
            return candidate.resolve()
    return None


def load_config(path: str | os.PathLike[str]) -> Configuration:
    """Load *path* as a strict version-1 project configuration."""

    config_path = Path(path).resolve()
    if not config_path.is_file():
        raise ConfigurationError(f"{config_path}: configuration must be a regular file")

    try:
        with config_path.open("rb") as stream:
            document = tomllib.load(stream)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"{config_path}: invalid TOML: {error}") from error
    except OSError as error:
        raise ConfigurationError(f"{config_path}: cannot read configuration: {error}") from error

    _reject_unknown_keys(config_path, document, _TOP_LEVEL_KEYS, "")
    version = document.get("format_version")
    if type(version) is not int or version != 1:
        raise _value_error(config_path, "format_version", "must be the integer 1")

    content = _table(config_path, document, "content")
    storage = _table(config_path, document, "storage")
    development = _table(config_path, document, "development")
    _reject_unknown_keys(config_path, content, _CONTENT_KEYS, "content")
    _reject_unknown_keys(config_path, storage, _STORAGE_KEYS, "storage")
    _reject_unknown_keys(config_path, development, _DEVELOPMENT_KEYS, "development")

    config_dir = config_path.parent
    content_default = config_dir / "content"
    if "paths" in content:
        content_paths = _path_list(config_path, config_dir, content["paths"], "content.paths")
    elif content_default.is_dir():
        content_paths = (content_default.resolve(),)
    else:
        content_paths = ()

    builtin_value = content.get("builtin", "include")
    if builtin_value not in ("include", "exclude"):
        raise _value_error(config_path, "content.builtin", "must be 'include' or 'exclude'")
    builtin: BuiltinMode = builtin_value

    map_paths = _one_or_many_paths(
        config_path,
        config_dir,
        storage.get("maps", "maps"),
        "storage.maps",
    )
    replay_paths = _one_or_many_paths(
        config_path,
        config_dir,
        storage.get("replays", "replays"),
        "storage.replays",
    )
    scenes_dir = _single_path(
        config_path,
        config_dir,
        storage.get("scenes", "scenes"),
        "storage.scenes",
    )
    encounters_dir = _single_path(
        config_path,
        config_dir,
        storage.get("encounters", "encounters"),
        "storage.encounters",
    )

    reload_value = development.get("reload", False)
    if type(reload_value) is not bool:
        raise _value_error(config_path, "development.reload", "must be a boolean")

    return Configuration(
        path=config_path,
        project_dir=config_dir.parent,
        content_paths=content_paths,
        builtin=builtin,
        map_paths=map_paths,
        replay_paths=replay_paths,
        scenes_dir=scenes_dir,
        encounters_dir=encounters_dir,
        reload=reload_value,
    )


def find_and_load_config(
    start: str | os.PathLike[str], explicit: str | os.PathLike[str] | None = None
) -> Configuration | None:
    """Load an explicit configuration or discover the nearest one from *start*."""

    if explicit is not None:
        return load_config(explicit)
    discovered = find_config(start)
    if discovered is None:
        return None
    return load_config(discovered)


def extract_config_argument(tokens: Sequence[str]) -> tuple[Path | None, list[str]]:
    """Remove one ``--config`` option from arbitrary command tokens."""

    explicit: Path | None = None
    remaining: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--config":
            if explicit is not None:
                raise ConfigurationError("--config may be specified only once")
            index += 1
            if index == len(tokens):
                raise ConfigurationError("--config requires a path")
            value = tokens[index]
            if not value.strip():
                raise ConfigurationError("--config requires a non-blank path")
            explicit = Path(value)
        elif token.startswith("--config="):
            if explicit is not None:
                raise ConfigurationError("--config may be specified only once")
            value = token.partition("=")[2]
            if not value.strip():
                raise ConfigurationError("--config requires a non-blank path")
            explicit = Path(value)
        else:
            remaining.append(token)
        index += 1
    return explicit, remaining


def apply_to_environment(config: Configuration, env: MutableMapping[str, str]) -> None:
    """Make *config* authoritative for legacy environment-based process consumers."""

    for name in LEGACY_PROJECT_ENVIRONMENT:
        env.pop(name, None)

    env[_PROJECT_ENV] = str(config.project_dir.resolve())
    env[_CONTENT_ENV] = (
        os.pathsep.join(str(path.resolve()) for path in config.content_paths)
        if config.content_paths
        else os.pathsep
    )
    env[_BUILTIN_ENV] = config.builtin
    env[_MAPS_ENV] = os.pathsep.join(str(path.resolve()) for path in config.map_paths)
    env[_REPLAYS_ENV] = os.pathsep.join(str(path.resolve()) for path in config.replay_paths)
    env[_SCENES_ENV] = str(config.scenes_dir.resolve())
    env[_ENCOUNTERS_ENV] = str(config.encounters_dir.resolve())
    if config.reload:
        env[_RELOAD_ENV] = "1"


def configuration_identity(config: Configuration) -> str:
    """A stable digest of the configuration's resolved meaning.

    Comments and TOML formatting do not restart a server. A changed path, mode,
    or development setting does, including when a relative path resolves
    differently because the same document moved to another project.
    """

    payload = {
        "format_version": 1,
        "project_dir": str(config.project_dir),
        "content_paths": [str(path) for path in config.content_paths],
        "builtin": config.builtin,
        "map_paths": [str(path) for path in config.map_paths],
        "replay_paths": [str(path) for path in config.replay_paths],
        "scenes_dir": str(config.scenes_dir),
        "encounters_dir": str(config.encounters_dir),
        "reload": config.reload,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _table(
    path: Path, document: Mapping[str, object], key: str
) -> Mapping[str, object]:
    value = document.get(key, {})
    if not isinstance(value, dict):
        raise _value_error(path, key, "must be a table")
    return value


def _reject_unknown_keys(
    path: Path,
    table: Mapping[str, object],
    allowed: frozenset[str],
    prefix: str,
) -> None:
    unknown = sorted(set(table) - allowed)
    if not unknown:
        return
    key = f"{prefix}.{unknown[0]}" if prefix else unknown[0]
    raise _value_error(path, key, "is not a recognized key")


def _path_list(path: Path, base: Path, value: object, key: str) -> tuple[Path, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _value_error(path, key, "must be a list of strings")
    return tuple(_resolve_path(path, base, item, key) for item in value)


def _one_or_many_paths(path: Path, base: Path, value: object, key: str) -> tuple[Path, ...]:
    if isinstance(value, str):
        return (_resolve_path(path, base, value, key),)
    resolved = _path_list(path, base, value, key)
    if not resolved:
        raise _value_error(path, key, "must contain at least one path")
    return resolved


def _single_path(path: Path, base: Path, value: object, key: str) -> Path:
    if not isinstance(value, str):
        raise _value_error(path, key, "must be a string")
    return _resolve_path(path, base, value, key)


def _resolve_path(path: Path, base: Path, value: str, key: str) -> Path:
    if not value.strip():
        raise _value_error(path, key, "must not be blank")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _value_error(path: Path, key: str, detail: str) -> ConfigurationError:
    return ConfigurationError(f"{path}: {key} {detail}")
