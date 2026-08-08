"""Deterministic, file-first staging for adventure play."""

from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = PLUGIN_ROOT / "scripts" / "fivee-play.py"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_fivee_play", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def helper() -> ModuleType:
    return _load_helper()


def test_helper_is_executable_and_distinguishes_prep_fallback(
    helper: ModuleType,
) -> None:
    assert HELPER_PATH.stat().st_mode & 0o111
    assert helper._compact_error(helper.PrepRequired("needs semantic prep")) == {
        "schema_version": 1,
        "status": "prep_required",
        "detail": "needs semantic prep",
    }


def test_atomic_publication_replaces_symlink_and_preserves_target_mode(
    helper: ModuleType, tmp_path: Path
) -> None:
    victim = tmp_path / "victim.json"
    victim.write_text('{"safe":true}\n', encoding="utf-8")
    target = tmp_path / "artifact.json"
    target.symlink_to(victim)

    helper._atomic_write_text(target, '{"generation":1}\n')

    assert victim.read_text(encoding="utf-8") == '{"safe":true}\n'
    assert not target.is_symlink()
    target.chmod(0o640)
    helper._atomic_write_text(target, '{"generation":2}\n')
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def _party_file(tmp_path: Path) -> Path:
    path = tmp_path / "parties.json"
    path.write_text(
        json.dumps(
            {
                "parties": {
                    "small": {
                        "description": "must not enter a projection",
                        "members": [
                            {
                                "class": "Fighter",
                                "species": "Human",
                                "background": "Guard",
                                "gear": ["rope"],
                                "rules": {"feature": "Second Wind"},
                                "temperament": "bold",
                                "voice": "terse",
                                "sheet": {
                                    "name": "Thora",
                                    "team": "party",
                                    "ac": 16,
                                    "max_hp": 12,
                                    "position": [0, 0],
                                },
                                "private_note": "never projected",
                            },
                            {
                                "class": "Rogue",
                                "species": "Halfling",
                                "background": "Scout",
                                "gear": ["chalk"],
                                "rules": {"feature": "Sneak Attack"},
                                "temperament": "cautious",
                                "voice": "quiet",
                                "sheet": {
                                    "name": "Kesh",
                                    "team": "party",
                                    "ac": 14,
                                    "max_hp": 9,
                                    "position": [5, 0],
                                },
                            },
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_party_selection_writes_three_distinct_privacy_projections(
    helper: ModuleType, tmp_path: Path
) -> None:
    selected = helper.select_party(_party_file(tmp_path), "small")
    engine, game_master, seats = helper.project_party(selected)

    assert engine == [member["sheet"] for member in selected]
    assert set(game_master) == {"members"}
    assert game_master["members"][0] == {
        "identity": "Thora",
        "class": "Fighter",
        "species": "Human",
        "background": "Guard",
        "gear": ["rope"],
        "rules": {"feature": "Second Wind"},
    }
    assert set(seats) == {"Thora", "Kesh"}
    assert seats["Thora"] == {
        "identity": "Thora",
        "sheet": selected[0]["sheet"],
        "gear": ["rope"],
        "rules": {"feature": "Second Wind"},
        "temperament": "bold",
        "voice": "terse",
    }
    serialized = json.dumps({"engine": engine, "gm": game_master, "seats": seats})
    assert "private_note" not in serialized
    assert "must not enter" not in serialized


@pytest.mark.parametrize(
    ("party_id", "match"),
    [("missing", "party id"), ("small", "duplicate member name")],
)
def test_party_selection_refuses_actionable_invalid_input(
    helper: ModuleType, tmp_path: Path, party_id: str, match: str
) -> None:
    path = _party_file(tmp_path)
    if party_id == "small":
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["parties"]["small"]["members"][1]["sheet"]["name"] = "Thora"
        path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(helper.PlaySetupError, match=match):
        helper.select_party(path, party_id)


def test_markdown_index_is_deterministic_source_ordered_and_resolves_links(
    helper: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "adventure.md"
    source.write_text(
        """# Chapter 1: Arrival

See [the ambush](#encounter-the-ambush).

Scene: The Gate
---------------

Description.

## Encounter: The Ambush

Fight.
""",
        encoding="utf-8",
    )

    first = helper.index_markdown(source)
    second = helper.index_markdown(source)

    assert first == second
    assert first["schema_version"] == 1
    assert first["source_sha256"] == helper.sha256_file(source)
    assert [entry["id"] for entry in first["entries"]] == ["m0001", "m0002", "m0003"]
    assert [entry["kind"] for entry in first["entries"]] == ["scene", "scene", "encounter"]
    assert first["entries"][0]["related_ids"] == ["m0003"]
    assert first["entries"][1]["locator"] == {"line_start": 5, "line_end": 9}


def test_markdown_index_falls_back_for_unresolved_or_ambiguous_structure(
    helper: ModuleType, tmp_path: Path
) -> None:
    unresolved = tmp_path / "unresolved.md"
    unresolved.write_text("# Scene: Start\n\n[missing](#nowhere)\n", encoding="utf-8")
    with pytest.raises(helper.PrepRequired, match="unresolved local link"):
        helper.index_markdown(unresolved)

    ambiguous = tmp_path / "ambiguous.md"
    ambiguous.write_text("plain prose with no structural headings\n", encoding="utf-8")
    with pytest.raises(helper.PrepRequired, match="structured Markdown"):
        helper.index_markdown(ambiguous)


def test_prepared_index_must_match_source_digest_and_complete_references(
    helper: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "adventure.md"
    source.write_text("# Scene: Start\n", encoding="utf-8")
    index = helper.index_markdown(source)
    helper.validate_module_index(index, source)

    index["source_sha256"] = "0" * 64
    with pytest.raises(helper.PlaySetupError, match="source digest"):
        helper.validate_module_index(index, source)

    index = helper.index_markdown(source)
    index["entries"][0]["related_ids"] = ["m9999"]
    with pytest.raises(helper.PlaySetupError, match="unknown related id"):
        helper.validate_module_index(index, source)


def test_index_cache_keys_source_digest_and_indexer_version(
    helper: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "adventure.md"
    source.write_text("# Scene: Start\n", encoding="utf-8")
    cache = tmp_path / "cache"

    first, first_status = helper.load_or_build_index(source, cache)
    second, second_status = helper.load_or_build_index(source, cache)
    source.write_text("# Scene: Changed\n", encoding="utf-8")
    third, third_status = helper.load_or_build_index(source, cache)

    assert first == second
    assert first_status == "built"
    assert second_status == "cached"
    assert third_status == "built"
    assert third["source_sha256"] != first["source_sha256"]
    assert len(list(cache.glob("*.json"))) == 2


def _fivee_adventure_source(tmp_path: Path) -> Path:
    path = tmp_path / "adventure-source.json"
    entries = [
        {
            "id": "chapter:running",
            "kind": "section",
            "role": "chapter",
            "title": "Running the adventure",
            "locator": {"line_start": 0, "line_end": 0},
            "related_ids": ["scene:A"],
            "content": [{"type": "p", "text": "Private GM context."}],
        },
        {
            "id": "scene:A",
            "kind": "area",
            "role": "area",
            "title": "The yard",
            "locator": {"line_start": 0, "line_end": 0},
            "related_ids": ["chapter:running"],
            "content": [
                {"type": "read-aloud", "tag": "Read aloud", "paras": ["A rain-dark yard."]}
            ],
            "play": {"light": "none"},
        },
        {
            "id": "reference:handout",
            "kind": "furniture",
            "role": "reference",
            "title": "The slate",
            "locator": {"line_start": 0, "line_end": 0},
            "related_ids": ["scene:A"],
            "content": {"handout_title": "The slate", "entries": "One, two."},
        },
    ]
    header = [
        "{",
        '  "format": "fivee-sim-adventure-source",',
        '  "format_version": 1,',
        '  "title": "Fixture Adventure",',
        '  "slug": "fixture-adventure",',
        '  "entries": [',
    ]
    cursor = len(header) + 1
    for entry in entries:
        rendered = json.dumps(entry, ensure_ascii=False, indent=2).splitlines()
        entry["locator"] = {
            "line_start": cursor,
            "line_end": cursor + len(rendered) - 1,
        }
        cursor += len(rendered) + 1
    lines = list(header)
    for position, entry in enumerate(entries):
        lines.extend(
            f"    {line}" for line in json.dumps(entry, ensure_ascii=False, indent=2).splitlines()
        )
        if position < len(entries) - 1:
            lines.append("    ,")
    lines.extend(["  ]", "}"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_native_fivee_source_builds_existing_private_index_without_markdown_prep(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _fivee_adventure_source(tmp_path)
    monkeypatch.setattr(
        helper,
        "index_markdown",
        lambda path: (_ for _ in ()).throw(AssertionError(f"prep path called for {path}")),
    )

    index, status = helper.load_or_build_index(source, tmp_path / "cache")

    assert status == "built"
    assert index["source_format"] == "fivee-sim-adventure-source"
    source_document = json.loads(source.read_text(encoding="utf-8"))
    assert [entry["id"] for entry in index["entries"]] == [
        "chapter:running",
        "scene:A",
        "reference:handout",
    ]
    assert index["entries"][1] == {
        "id": "scene:A",
        "kind": "scene",
        "title": "The yard",
        "locator": source_document["entries"][1]["locator"],
        "related_ids": ["chapter:running"],
    }
    assert index["entries"][0]["locator"]["line_end"] > index["entries"][0]["locator"][
        "line_start"
    ]
    assert index["entries"][2]["kind"] == "reference"
    assert helper.INDEXER_VERSION == 2


def test_arbitrary_json_retains_prep_fallback(helper: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "arbitrary.json"
    source.write_text('{"chapters": []}\n', encoding="utf-8")

    with pytest.raises(helper.PrepRequired, match="structured Markdown only"):
        helper.load_or_build_index(source, tmp_path / "cache")


def test_recognized_native_source_refuses_unsupported_version(
    helper: ModuleType, tmp_path: Path
) -> None:
    source = _fivee_adventure_source(tmp_path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["format_version"] = 2
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(helper.PlaySetupError, match="unsupported.*format_version 2"):
        helper.load_or_build_index(source, tmp_path / "cache")


def test_native_source_locator_must_exactly_cover_its_serialized_entry(
    helper: ModuleType, tmp_path: Path
) -> None:
    source = _fivee_adventure_source(tmp_path)
    lines = source.read_text(encoding="utf-8").splitlines()
    document = json.loads(source.read_text(encoding="utf-8"))
    first = document["entries"][0]
    start = first["locator"]["line_start"]
    end = first["locator"]["line_end"]
    first["locator"]["line_end"] = end + 1
    replacement = [
        f"    {line}" for line in json.dumps(first, ensure_ascii=False, indent=2).splitlines()
    ]
    assert len(replacement) == end - start + 1
    lines[start - 1 : end] = replacement
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(helper.PlaySetupError, match="locator.*exact serialized entry"):
        helper.load_or_build_index(source, tmp_path / "cache")


def test_native_source_does_not_bypass_playtest_semantic_inventory_gate(
    helper: ModuleType, tmp_path: Path
) -> None:
    source = _fivee_adventure_source(tmp_path)
    runner = FakeRunner()

    with pytest.raises(helper.PrepRequired, match="matching semantic inventory"):
        helper.init_play(
            config_path=_project_config(tmp_path),
            adventure_path=source,
            mode="playtest",
            seed=42,
            gm_kind="agent",
            seat_kinds={},
            party_file=_party_file(tmp_path),
            party_id="small",
            selected_names=None,
            prepared_index=None,
            playtest_inventory=None,
            opening_scene=None,
            runner=runner,
            jq_path=Path("/usr/bin/jq"),
        )

    assert runner.calls == []


def test_publish_prep_validates_manifest_and_is_idempotent(
    helper: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "adventure.md"
    source.write_text("# Scene: Start\n", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    partial = staging / "module-index.json.partial"
    partial.write_text(json.dumps(helper.index_markdown(source)), encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "complete": True,
        "source_sha256": helper.sha256_file(source),
        "files": [
            {
                "path": partial.name,
                "publish_as": "module-index.json",
                "sha256": helper.sha256_file(partial),
                "kind": "module-index",
            }
        ],
    }
    manifest_path = staging / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    destination = tmp_path / "published"

    first = helper.publish_prep(staging, manifest_path, destination, source)
    second = helper.publish_prep(staging, manifest_path, destination, source)

    assert first["status"] == "published"
    assert second["status"] == "reused"
    assert (destination / "module-index.json").is_file()
    assert not list(destination.glob("*.partial"))


def test_publish_prep_refuses_digest_mismatch_without_partial_publication(
    helper: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "adventure.md"
    source.write_text("# Scene: Start\n", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    partial = staging / "module-index.json.partial"
    partial.write_text(json.dumps(helper.index_markdown(source)), encoding="utf-8")
    manifest_path = staging / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "complete": True,
                "source_sha256": helper.sha256_file(source),
                "files": [
                    {
                        "path": partial.name,
                        "publish_as": "module-index.json",
                        "sha256": "0" * 64,
                        "kind": "module-index",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    destination = tmp_path / "published"

    with pytest.raises(helper.PlaySetupError, match="digest"):
        helper.publish_prep(staging, manifest_path, destination, source)

    assert not destination.exists()


def test_publish_prep_refuses_inventory_for_another_adventure(
    helper: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "adventure.md"
    source.write_text("# Scene: Start\n", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    partial = staging / "run-sheet.json.partial"
    partial.write_text(
        json.dumps({"schema_version": 1, "source_sha256": "0" * 64}),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "complete": True,
        "source_sha256": helper.sha256_file(source),
        "files": [
            {
                "path": partial.name,
                "publish_as": "run-sheet.json",
                "sha256": helper.sha256_file(partial),
                "kind": "playtest-inventory",
            }
        ],
    }
    manifest_path = staging / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(helper.PlaySetupError, match="inventory source digest"):
        helper.publish_prep(staging, manifest_path, tmp_path / "published", source)

    assert not (tmp_path / "published").exists()


def test_roster_loader_keeps_v1_inline_runs_and_resolves_v2_references(
    helper: ModuleType, tmp_path: Path
) -> None:
    v1 = tmp_path / "v1.json"
    inline = {"mode": "play", "seats": [{"name": "Thora", "sheet": {"ac": 16}}]}
    v1.write_text(json.dumps(inline), encoding="utf-8")
    assert helper.load_roster(v1) == inline

    run = tmp_path / "run"
    (run / "inputs" / "seats").mkdir(parents=True)
    seat = {"identity": "Thora", "sheet": {"name": "Thora", "ac": 16}}
    (run / "inputs" / "seats" / "thora.json").write_text(json.dumps(seat), encoding="utf-8")
    roster = {
        "schema_version": 2,
        "mode": "play",
        "seats": [{"name": "Thora", "input": "inputs/seats/thora.json"}],
    }
    roster_path = run / "roster.json"
    roster_path.write_text(json.dumps(roster), encoding="utf-8")

    loaded = helper.load_roster(roster_path)
    assert loaded["seats"][0]["input_data"] == seat
    assert json.loads(roster_path.read_text(encoding="utf-8")) == roster


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.openings: list[dict[str, Any]] = []

    def run(self, tokens: list[str]) -> dict[str, Any]:
        self.calls.append(tokens)
        if "serve" in tokens:
            return {"runtime_dir": "/runtime", "already_running": False}
        if "content.status" in tokens:
            return {
                "generation": 1,
                "counts": {"creatures": 2},
                "packs": [{"id": "fixture"}],
                "configured_paths": ["secret-content-path"],
            }
        if "adventure.create" in tokens:
            return {"adventure_id": "adv-7", "version": "1", "status": "active"}
        raise AssertionError(tokens)

    def opening_chapter(self, **values: Any) -> dict[str, Any]:
        self.openings.append(values)
        return {
            "adventure_id": "adv-7",
            "encounter_id": "enc-9",
            "index": 0,
            "version": "2",
            "state_sha256": "a" * 64,
            "map_sha256": None,
        }


def _project_config(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    root = project / ".fivee-sim"
    for name in ("content", "maps", "scenes"):
        (root / name).mkdir(parents=True, exist_ok=True)
    config = root / "config.toml"
    config.write_text(
        """format_version = 1
[content]
paths = ["content"]
builtin = "exclude"
[storage]
maps = "maps"
scenes = "scenes"
runs = "runs"
""",
        encoding="utf-8",
    )
    return config


def test_init_stages_v2_artifacts_and_returns_only_compact_metadata(
    helper: ModuleType, tmp_path: Path
) -> None:
    adventure = tmp_path / "project" / "adventure.md"
    adventure.parent.mkdir(parents=True, exist_ok=True)
    adventure.write_text("# Scene: Opening\n", encoding="utf-8")
    party = _party_file(tmp_path)
    runner = FakeRunner()

    result = helper.init_play(
        config_path=_project_config(tmp_path),
        adventure_path=adventure,
        mode="play",
        seed=42,
        gm_kind="agent",
        seat_kinds={"Thora": "agent", "Kesh": "human"},
        party_file=party,
        party_id="small",
        selected_names=None,
        prepared_index=None,
        playtest_inventory=None,
        opening_scene=None,
        runner=runner,
        jq_path=Path("/usr/bin/jq"),
    )

    run = tmp_path / "project" / ".fivee-sim" / "plays" / "adv-7"
    assert result == {
        "schema_version": 1,
        "status": "ready",
        "mode": "play",
        "adventure_id": "adv-7",
        "artifact_id": "adv-7",
        "adventure_version": "1",
        "source_sha256": helper.sha256_file(adventure),
        "module_index_sha256": helper.sha256_file(run / "module-index.json"),
        "content_generation": 1,
        "content_counts": {"creatures": 2},
        "seat_count": 2,
        "paths": {
            "play": str(run),
            "roster": str(run / "roster.json"),
            "checkpoint": str(run / "checkpoint.json"),
        },
    }
    assert "secret-content-path" not in json.dumps(result)
    assert "Thora" not in json.dumps(result)

    roster = json.loads((run / "roster.json").read_text(encoding="utf-8"))
    assert roster["schema_version"] == 2
    assert roster["party_engine"] == "inputs/party-engine.json"
    assert roster["party_gm"] == "inputs/party-gm.json"
    assert [seat["kind"] for seat in roster["seats"]] == ["agent", "human"]
    assert json.loads((run / "inputs/party-engine.json").read_text(encoding="utf-8")) == [
        member["sheet"] for member in helper.select_party(party, "small")
    ]
    assert (run / "transcript.md").read_text(encoding="utf-8") == ""
    for relative in (
        "checkpoint.json",
        "council.json",
        "brief-cursors.json",
        "inputs/party-gm.json",
        "inputs/seats/thora.json",
        "inputs/seats/kesh.json",
        "seats/thora.md",
        "seats/kesh.md",
    ):
        assert (run / relative).is_file(), relative

    operations = [
        next(token for token in call if token in {"serve", "content.status", "adventure.create"})
        for call in runner.calls
    ]
    assert operations == ["serve", "content.status", "adventure.create"]
    create = runner.calls[-1]
    assert "--idempotency-key" in create
    assert "--select" in create


def test_init_pipeline_receives_paths_not_party_or_scene_bodies(
    helper: ModuleType, tmp_path: Path
) -> None:
    adventure = tmp_path / "project" / "adventure.md"
    adventure.parent.mkdir(parents=True, exist_ok=True)
    adventure.write_text("# Scene: Opening\n", encoding="utf-8")
    runner = FakeRunner()

    result = helper.init_play(
        config_path=_project_config(tmp_path),
        adventure_path=adventure,
        mode="play",
        seed=42,
        gm_kind="agent",
        seat_kinds={},
        party_file=_party_file(tmp_path),
        party_id="small",
        selected_names=None,
        prepared_index=None,
        playtest_inventory=None,
        opening_scene="opening",
        runner=runner,
        jq_path=Path("/usr/bin/jq"),
    )

    assert result["encounter_id"] == "enc-9"
    assert result["adventure_version"] == "2"
    assert len(runner.openings) == 1
    opening = runner.openings[0]
    assert opening["scene_id"] == "opening"
    assert opening["party_engine_path"].name == "party-engine.json"
    assert opening["selected_names"] == ["Thora", "Kesh"]
    assert "mode" not in opening
    assert "combatants" not in opening
    assert "max_hp" not in repr(opening)


def test_init_retry_reuses_saved_run_without_rewriting_artifacts(
    helper: ModuleType, tmp_path: Path
) -> None:
    adventure = tmp_path / "project" / "adventure.md"
    adventure.parent.mkdir(parents=True, exist_ok=True)
    adventure.write_text("# Scene: Opening\n", encoding="utf-8")
    config = _project_config(tmp_path)
    party = _party_file(tmp_path)
    runner = FakeRunner()
    arguments = {
        "config_path": config,
        "adventure_path": adventure,
        "mode": "play",
        "seed": 42,
        "gm_kind": "agent",
        "seat_kinds": {},
        "party_file": party,
        "party_id": "small",
        "selected_names": None,
        "prepared_index": None,
        "playtest_inventory": None,
        "opening_scene": "opening",
        "runner": runner,
        "jq_path": Path("/usr/bin/jq"),
    }
    first = helper.init_play(**arguments)
    run = tmp_path / "project" / ".fivee-sim" / "plays" / "adv-7"
    transcript = run / "transcript.md"
    seat_memory = run / "seats" / "thora.md"
    transcript.write_text("existing transcript\n", encoding="utf-8")
    seat_memory.write_text("existing memory\n", encoding="utf-8")

    second = helper.init_play(**arguments)

    assert first["status"] == "ready"
    assert second["status"] == "reused"
    assert second["adventure_version"] == "2"
    assert second["encounter_id"] == "enc-9"
    assert transcript.read_text(encoding="utf-8") == "existing transcript\n"
    assert seat_memory.read_text(encoding="utf-8") == "existing memory\n"
    assert len(runner.openings) == 1


def test_init_refuses_nonfinal_configured_inputs_before_engine_calls(
    helper: ModuleType, tmp_path: Path
) -> None:
    config = _project_config(tmp_path)
    (config.parent / "scenes").rmdir()
    adventure = tmp_path / "project" / "adventure.md"
    adventure.write_text("# Scene: Opening\n", encoding="utf-8")
    runner = FakeRunner()

    with pytest.raises(helper.PlaySetupError, match="configured scenes directory"):
        helper.init_play(
            config_path=config,
            adventure_path=adventure,
            mode="play",
            seed=1,
            gm_kind="agent",
            seat_kinds={},
            party_file=_party_file(tmp_path),
            party_id="small",
            selected_names=None,
            prepared_index=None,
            playtest_inventory=None,
            opening_scene=None,
            runner=runner,
            jq_path=Path("/usr/bin/jq"),
        )

    assert runner.calls == []
