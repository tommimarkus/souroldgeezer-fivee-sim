"""The replay *read* side of the service layer: rooting, listing, loading.

Replays until now were write-only — ``replay_export`` put a file on disk and
nothing ever read one back. Both adapters now need to, so the reading lives
here rather than in either of them, mirroring ``service.maps``'s
``maps_root`` / ``list_maps`` / ``load_file`` trio exactly.

Two claims are worth stating out loud because they are what a listing gets
wrong. A listing is **not a validator**: a file that is not a replay bundle is
skipped silently, because the job is to show what is playable, not to grade
the directory. A *load*, by contrast, refuses anything the viewer would choke
on, and refuses it with the same diagnostics ``replay_validate`` reports — one
validator, one vocabulary, whichever surface asked.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.mcp_server import server as api
from fivee_sim.service.errors import ReplayError
from fivee_sim.service.replay import (
    REPLAYS_ENV,
    environment_replay_roots,
    list_replays,
    load_bundle_file,
    replays_root,
    sha256_bytes,
)

from .conftest import mapless_fight


def exported(target: Path, *, seed: int, format_version: int = 2) -> dict[str, Any]:
    """A real bundle on disk at ``target``, by the tool that writes them.

    Hand-built fixtures would drift from the exporter; these cannot, which is
    the point — the listing has to keep working against what actually gets
    written, not against what a test thinks gets written.
    """
    encounter_id = mapless_fight(seed=seed)
    api.replay_export(encounter_id, path=str(target), format_version=format_version)
    bundle: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
    return bundle


class TestReplaysRoot:
    """Where replays live, resolved exactly as maps and encounters are."""

    def test_the_replays_environment_variable_wins_outright(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(REPLAYS_ENV, str(tmp_path / "elsewhere"))
        monkeypatch.setenv("FIVEE_SIM_PROJECT_DIR", str(tmp_path / "project"))

        assert replays_root() == tmp_path / "elsewhere"

    def test_several_configured_roots_are_split_on_the_path_separator(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        first, second = tmp_path / "one", tmp_path / "two"
        monkeypatch.setenv(REPLAYS_ENV, os.pathsep.join([str(first), str(second)]))

        assert environment_replay_roots() == [str(first), str(second)]
        # The root a *write* goes to is the first, as with maps.
        assert replays_root() == first

    def test_the_project_directory_puts_replays_beside_maps_not_inside_them(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv(REPLAYS_ENV, raising=False)
        monkeypatch.setenv("FIVEE_SIM_PROJECT_DIR", str(tmp_path))

        assert replays_root() == tmp_path / ".fivee-sim" / "replays"

    def test_with_nothing_configured_the_root_is_under_the_working_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv(REPLAYS_ENV, raising=False)
        monkeypatch.delenv("FIVEE_SIM_PROJECT_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.chdir(tmp_path)

        assert replays_root() == Path.cwd() / ".fivee-sim" / "replays"


class TestListReplays:
    """A catalogue row per playable bundle, and silence about everything else."""

    def test_a_v2_bundle_is_listed_with_what_a_chooser_needs(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "gatehouse.json"
        bundle = exported(target, seed=61)

        (row,) = list_replays([tmp_path])

        assert row["path"] == str(target)
        assert row["name"] == bundle["name"]
        assert row["seed"] == bundle["seed"]
        assert row["format_version"] == 2
        assert row["events"] == len(bundle["events"])
        # Lifted out of the nested ``encounter`` block, because a chooser wants
        # one flat row and should not have to know v2's envelope shape.
        assert row["encounter_id"] == bundle["encounter"]["id"]

    def test_a_v1_bundle_is_listed_too_and_reports_no_encounter_id(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "legacy.json"
        exported(target, seed=62, format_version=1)

        (row,) = list_replays([tmp_path])

        assert row["format_version"] == 1
        assert row["encounter_id"] is None

    def test_the_row_carries_the_file_hash_so_a_caller_can_tag_it(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "hashed.json"
        exported(target, seed=63)

        (row,) = list_replays([tmp_path])

        assert row["sha256"] == sha256_bytes(target.read_bytes())

    def test_files_that_are_not_replay_bundles_are_skipped_in_silence(
        self, tmp_path: Path
    ) -> None:
        exported(tmp_path / "real.json", seed=64)
        (tmp_path / "notes.json").write_text('{"format": "something-else"}', "utf-8")
        (tmp_path / "broken.json").write_text("{ not json", encoding="utf-8")
        (tmp_path / "array.json").write_text("[1, 2, 3]", encoding="utf-8")
        (tmp_path / "map.json").write_text('{"format": "fivee-sim-map"}', "utf-8")

        assert [Path(row["path"]).name for row in list_replays([tmp_path])] == [
            "real.json"
        ]

    def test_an_unplayable_bundle_is_still_listed_because_listing_is_not_grading(
        self, tmp_path: Path
    ) -> None:
        """A row is a directory entry. ``load_bundle_file`` is where a corrupt
        bundle gets named — listing it and refusing to load it is how the user
        learns *which* file is broken, instead of it vanishing from the list."""
        target = tmp_path / "corrupt.json"
        bundle = exported(target, seed=65)
        bundle["events"] = "not a list"
        target.write_text(json.dumps(bundle), encoding="utf-8")

        assert [Path(row["path"]).name for row in list_replays([tmp_path])] == [
            "corrupt.json"
        ]

    def test_rows_are_sorted_by_path_so_the_order_is_stable_across_runs(
        self, tmp_path: Path
    ) -> None:
        exported(tmp_path / "zulu.json", seed=66)
        exported(tmp_path / "alpha.json", seed=67)

        assert [Path(row["path"]).name for row in list_replays([tmp_path])] == [
            "alpha.json",
            "zulu.json",
        ]

    def test_with_no_roots_given_the_configured_root_is_listed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(REPLAYS_ENV, str(tmp_path))
        exported(tmp_path / "configured.json", seed=68)

        assert [Path(row["path"]).name for row in list_replays()] == [
            "configured.json"
        ]

    def test_a_directory_that_does_not_exist_lists_as_empty_not_as_an_error(
        self, tmp_path: Path
    ) -> None:
        assert list_replays([tmp_path / "no-such-directory"]) == []


class TestLoadBundleFile:
    """The load refuses what the viewer could not play, in the tool's own words."""

    def test_a_valid_bundle_comes_back_parsed(self, tmp_path: Path) -> None:
        target = tmp_path / "playable.json"
        bundle = exported(target, seed=71)

        assert load_bundle_file(target) == bundle

    def test_an_invalid_bundle_is_refused_with_the_validator_diagnostics(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "corrupt.json"
        bundle = exported(target, seed=72)
        del bundle["events"]
        target.write_text(json.dumps(bundle), encoding="utf-8")

        with pytest.raises(ReplayError, match="corrupt.json") as raised:
            load_bundle_file(target)

        # The same diagnostic shape replay_validate reports, so an adapter can
        # forward it without inventing a second vocabulary.
        assert raised.value.diagnostics
        assert {"path", "problem"} <= set(raised.value.diagnostics[0])

    def test_a_file_that_is_not_json_is_refused_by_name(self, tmp_path: Path) -> None:
        target = tmp_path / "prose.json"
        target.write_text("this is not a replay", encoding="utf-8")

        with pytest.raises(ReplayError, match="is not valid JSON"):
            load_bundle_file(target)

    def test_a_missing_file_is_refused_rather_than_raising_oserror(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ReplayError, match="cannot be read"):
            load_bundle_file(tmp_path / "absent.json")
