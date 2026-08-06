from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from fivee_sim import paths
from fivee_sim.configuration import (
    _STORAGE_KEYS,
    CONFIG_SUBPATH,
    ConfigurationError,
    apply_to_environment,
    configuration_identity,
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
adventures = "adventures"
blobs = "blobs"

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
    assert config.adventures_dir == (config_path.parent / "adventures").resolve()
    assert config.blobs_dir == (config_path.parent / "blobs").resolve()
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
    assert without_content.adventures_dir == config_dir / "adventures"
    assert without_content.blobs_dir == config_dir / "blobs"
    assert without_content.reload is False

    (config_path.parent / "content").mkdir()
    with_content = load_config(config_path)
    assert with_content.content_paths == (config_dir / "content",)


def test_configuration_identity_tracks_meaning_not_toml_formatting(tmp_path: Path) -> None:
    first = _write_config(
        tmp_path,
        "format_version=1\n[storage]\nmaps='maps'\n",
    )
    before = configuration_identity(load_config(first))

    first.write_text(
        """\
# The same configuration with ordinary human formatting.
format_version = 1

[storage]
maps = "maps"
""",
        encoding="utf-8",
    )
    assert configuration_identity(load_config(first)) == before

    first.write_text(
        "format_version = 1\n[storage]\nmaps = 'other-maps'\n",
        encoding="utf-8",
    )
    assert configuration_identity(load_config(first)) != before


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
        ("format_version = 1\n[storage]\nmaps = []\n", "storage.maps"),
        ("format_version = 1\n[storage]\nreplays = [\"ok\", 1]\n", "storage.replays"),
        ("format_version = 1\n[storage]\nreplays = []\n", "storage.replays"),
        ("format_version = 1\n[storage]\nscenes = []\n", "storage.scenes"),
        ("format_version = 1\n[storage]\nencounters = false\n", "storage.encounters"),
        ("format_version = 1\n[storage]\nblobs = false\n", "storage.blobs"),
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
adventures = "adventures"
blobs = "blobs"
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
        "FIVEE_SIM_ADVENTURES",
        "FIVEE_SIM_BLOBS",
        "FIVEE_SIM_RELOAD",
    }
    environment = dict.fromkeys(legacy_names, "stale")
    environment["KEEP_ME"] = "yes"

    apply_to_environment(config, environment)

    assert environment == {
        "KEEP_ME": "yes",
        "FIVEE_SIM_PROJECT_DIR": str(tmp_path.resolve()),
        # A non-blank separator with no entries is the legacy adapter's explicit
        # empty search path. Omitting the variable would re-enable the project's
        # sibling content directory and make paths = [] a lie.
        "FIVEE_SIM_CONTENT": os.pathsep,
        "FIVEE_SIM_BUILTIN": "exclude",
        "FIVEE_SIM_MAPS": os.pathsep.join(str(path) for path in config.map_paths),
        "FIVEE_SIM_REPLAYS": os.pathsep.join(str(path) for path in config.replay_paths),
        "FIVEE_SIM_SCENES": str(config.scenes_dir),
        "FIVEE_SIM_ENCOUNTERS": str(config.encounters_dir),
        "FIVEE_SIM_ADVENTURES": str(config.adventures_dir),
        "FIVEE_SIM_BLOBS": str(config.blobs_dir),
        "FIVEE_SIM_RELOAD": "1",
    }


def test_apply_to_environment_leaves_reload_unset_when_disabled(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, "format_version = 1\n"))
    environment = {"FIVEE_SIM_RELOAD": "1"}

    apply_to_environment(config, environment)

    assert "FIVEE_SIM_RELOAD" not in environment


# --- every storage key is plumbed the whole way ------------------------------
#
# A root added to ``paths.py`` alone is silently unconfigurable, and worse:
# ``_reject_unknown_keys`` actively refuses the ``[storage]`` line an author
# would write for it. Nothing else notices, because every other test names the
# four keys that already worked. So both tests below *derive* the keys from
# ``_STORAGE_KEYS`` — the one declaration the parser, the adapter and the
# identity digest are all held against — rather than listing them, and each
# opens by asserting the declaration is non-empty, so a walk that had nothing
# to check cannot pass by finding nothing wrong.
#
# That opening assertion is the whole of the vacuity guard. Each of these used
# to also close with ``assert visited == set(keys)`` over a set the loop filled
# unconditionally on every iteration, which is a restatement of the loop rather
# than a check on it: no reachable change to the code under test could make it
# fail, and it read as a second guarantee while being none.


def _storage_config(project: Path, values: Mapping[str, str]) -> Path:
    lines = "\n".join(f'{key} = "{value}"' for key, value in sorted(values.items()))
    return _write_config(project, f"format_version = 1\n\n[storage]\n{lines}\n")


def test_every_storage_key_reaches_the_root_the_engine_resolves(tmp_path: Path) -> None:
    """A key the file accepts must arrive at the directory the engine reads.

    Three sites in a row, and a break in any of them is quiet: the parser has
    to allow the key, ``apply_to_environment`` has to export it, and
    ``paths.<key>_root`` has to read that variable back. A key that stops
    halfway is accepted, resolved, and then ignored.
    """
    keys = sorted(_STORAGE_KEYS)
    assert keys, "there is nothing to check; _STORAGE_KEYS is the declaration"
    config = load_config(_storage_config(tmp_path, {key: f"{key}-dir" for key in keys}))

    environment: dict[str, str] = {}
    apply_to_environment(config, environment)

    for key in keys:
        variable = f"FIVEE_SIM_{key.upper()}"
        assert variable in environment, (
            f"storage.{key} is parsed but never exported as {variable}; "
            f"apply_to_environment is the site"
        )
        root = getattr(paths, f"{key}_root", None)
        assert root is not None, f"paths.py has no {key}_root for storage.{key}"
        assert root({variable: environment[variable]}) == (
            tmp_path / ".fivee-sim" / f"{key}-dir"
        ).resolve(), f"{key}_root does not read {variable}"


def test_moving_any_storage_key_moves_the_configuration_identity(tmp_path: Path) -> None:
    """A path the digest forgets is a server nobody replaces when it changes.

    ``configuration_identity`` is what tells a launcher the running process was
    started for a different project layout. A storage key missing from its
    payload leaves the old server holding the old directory, answering as if
    nothing had moved.
    """
    keys = sorted(_STORAGE_KEYS)
    assert keys, "there is nothing to check; _STORAGE_KEYS is the declaration"
    settled = {key: f"{key}-dir" for key in keys}
    base = configuration_identity(load_config(_storage_config(tmp_path, settled)))

    for key in keys:
        moved = configuration_identity(
            load_config(_storage_config(tmp_path, {**settled, key: f"{key}-elsewhere"}))
        )
        assert moved != base, (
            f"moving storage.{key} left configuration_identity unchanged; "
            f"the digest's payload is the site"
        )


#: The three documents that spell the ``[storage]`` table out for a reader, and
#: so the three that go silently stale when a key joins it. They are checked
#: against :data:`_STORAGE_KEYS` rather than against each other: one declaration,
#: three renderings, which is the same arrangement the coverage and rulings
#: reports are held to.
_STORAGE_DOCS = (
    Path(__file__).resolve().parents[3] / "README.md",
    Path(__file__).resolve().parents[2] / "docs" / "MAPS.md",
    Path(__file__).resolve().parents[2] / "skills" / "map-forge" / "SKILL.md",
)


def test_every_storage_key_is_written_down_where_a_reader_would_look() -> None:
    """A configurable root nobody documented is a root nobody configures.

    ``_reject_unknown_keys`` makes the failure sharp — a ``[storage]`` line for
    an undocumented key is refused as unrecognized, so a reader working from the
    published table cannot even discover the setting by guessing at it. This is
    the checkpoint enforced by nothing until now; it was found by grep, and grep
    is not something the next person will think to run.

    The key has to appear *as a setting*, not as a word. A bare ``key in text``
    was satisfied by any prose containing "maps" or "scenes" — which all three
    of these documents contain for unrelated reasons — so it would have gone on
    passing for a key nobody had documented at all. What counts is a TOML
    assignment in the ``[storage]`` block these documents each print, or the key
    as a code span, optionally qualified: the three forms the documents actually
    use between them.
    """
    keys = sorted(_STORAGE_KEYS)
    assert keys, "there is nothing to check; _STORAGE_KEYS is the declaration"
    for document in _STORAGE_DOCS:
        assert document.is_file(), f"{document} is not where this test looks for it"
        text = document.read_text(encoding="utf-8")
        for key in keys:
            spelled = re.compile(
                rf"(?m)`(?:\[storage\]\.|storage\.)?{re.escape(key)}`|^{re.escape(key)} = "
            )
            assert spelled.search(text), (
                f"{document.name} never spells storage.{key} as a setting; "
                f"a reader cannot configure a root that is only mentioned in prose"
            )
        # And the legacy variable, which every one of these three also lists —
        # the compatibility fallback a caller reaches for when no file is
        # selected, and the half most easily forgotten.
        for key in keys:
            variable = f"FIVEE_SIM_{key.upper()}"
            assert variable in text, f"{document.name} never mentions {variable}"
