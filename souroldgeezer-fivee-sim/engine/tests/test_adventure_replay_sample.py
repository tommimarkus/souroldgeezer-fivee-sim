"""The checked-in three-chapter adventure replay showcase generator."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from fivee_sim import adventure_replay_sample
from fivee_sim.service import adventures
from fivee_sim.service import replay as replay_service


def embedded_bundle(html: str) -> dict[str, Any]:
    found = re.findall(
        r'<script type="application/json" id="embedded-data">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert len(found) == 1
    parsed = json.loads(found[0])
    assert isinstance(parsed, dict)
    return parsed


def combatant(state: dict[str, Any], name: str) -> dict[str, Any]:
    return next(one for one in state["combatants"] if one["name"] == name)


def carried_state(state: dict[str, Any], name: str) -> dict[str, Any]:
    found = combatant(state, name)
    return {key: found.get(key) for key in adventures.CARRIED_STATE_KEYS}


def test_the_adventure_showcase_is_a_meaningful_three_chapter_run() -> None:
    bundle = adventure_replay_sample.sample_bundle()

    assert bundle["format"] == "fivee-sim-adventure-replay"
    assert bundle["format_version"] == 1
    assert bundle["adventure"]["id"] == "adventure-showcase"
    assert bundle["adventure"]["name"] == "The Gatehouse Run"
    assert replay_service.validate_adventure_replay(bundle) == []

    chapters = bundle["chapters"]
    assert [chapter["index"] for chapter in chapters] == [0, 1, 2]
    assert [chapter["mode"] for chapter in chapters] == [
        "exploration",
        "combat",
        "exploration",
    ]
    assert [chapter["replay"]["name"] for chapter in chapters] == [
        "Arrival at the Gatehouse",
        "Gatehouse Victory Showcase",
        "Aftermath at the Gatehouse",
    ]
    assert [chapter["carried"] for chapter in chapters] == [
        [],
        ["Arin", "Mira"],
        ["Arin", "Mira"],
    ]

    event_counts = [len(chapter["replay"]["events"]) for chapter in chapters]
    assert event_counts[0] >= 5
    assert event_counts[1] >= 30
    assert event_counts[2] >= 5
    assert sum(event_counts) >= 40
    assert all(
        replay_service.validate_replay(chapter["replay"]) == []
        for chapter in chapters
    )


def test_the_party_reaches_each_chapter_where_the_previous_one_left_it() -> None:
    chapters = adventure_replay_sample.sample_bundle()["chapters"]

    for previous, following in zip(chapters[:-1], chapters[1:], strict=True):
        for name in ("Arin", "Mira"):
            expected = carried_state(previous["replay"]["latest_state"], name)
            expected.update(following.get("recovery", {}).get(name, {}))
            assert expected == carried_state(
                following["replay"]["initial"]["state"], name
            ), f"{name}'s carried state breaks before chapter {following['index'] + 1}"


def test_the_showcase_demonstrates_a_caller_stated_rest_boundary() -> None:
    _, _, aftermath = adventure_replay_sample.sample_bundle()["chapters"]

    assert aftermath["recovery_note"] == "Short rest after the gatehouse battle"
    assert aftermath["recovery"] == {"Arin": {"hp": 12}}
    assert combatant(aftermath["replay"]["initial"]["state"], "Arin")["hp"] == 12


def test_the_interludes_show_arrival_and_aftermath_rather_than_padding() -> None:
    arrival, _, aftermath = adventure_replay_sample.sample_bundle()["chapters"]
    arrival_kinds = {event["kind"] for event in arrival["replay"]["events"]}
    aftermath_kinds = {event["kind"] for event in aftermath["replay"]["events"]}

    assert "move" in arrival_kinds
    assert {"move", "interact", "heal"} <= aftermath_kinds
    assert any(
        attempt["operation"] == "check" and attempt["status"] == "success"
        for attempt in arrival["replay"]["attempts"]
    )
    assert any(
        attempt["operation"] == "encounter_note"
        and attempt["status"] == "success"
        and attempt["arguments"].get("speaker") == "Mira"
        for attempt in arrival["replay"]["attempts"]
        + aftermath["replay"]["attempts"]
    )
    outcome = aftermath["replay"]["attempts"][-1]
    assert outcome["operation"] == "encounter_note"
    assert outcome["status"] == "success"
    assert "secured" in outcome["arguments"]["text"].lower()


def test_each_interlude_frozen_state_agrees_with_its_event_log() -> None:
    arrival, _, aftermath = adventure_replay_sample.sample_bundle()["chapters"]

    for chapter in (arrival, aftermath):
        replay = chapter["replay"]
        initial = replay["initial"]["state"]
        final = replay["latest_state"]
        for name in ("Arin", "Mira"):
            moves = [
                event
                for event in replay["events"]
                if event["kind"] == "move" and event["actor"] == name
            ]
            expected_position = (
                moves[-1]["data"]["destination"]
                if moves
                else combatant(initial, name)["position"]
            )
            assert combatant(final, name)["position"] == expected_position
            level_moves = [event for event in moves if "to_level" in event["data"]]
            expected_level = (
                level_moves[-1]["data"]["to_level"]
                if level_moves
                else combatant(initial, name)["level"]
            )
            assert combatant(final, name)["level"] == expected_level

        heals = [event for event in replay["events"] if event["kind"] == "heal"]
        for event in heals:
            assert combatant(final, event["target"])["hp"] == event["data"]["hp"]
        interactions = [
            event for event in replay["events"] if event["kind"] == "interact"
        ]
        for event in interactions[-1:]:
            feature = event["data"]["feature"]
            assert final["map"]["features"][feature]["open"] is event["data"]["open"]


def test_the_adventure_showcase_writes_one_self_contained_html_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "adventure-showcase.html"

    result = adventure_replay_sample.write_sample(target)

    assert result == target
    html = target.read_text(encoding="utf-8")
    bundle = embedded_bundle(html)
    assert bundle["format"] == "fivee-sim-adventure-replay"
    assert len(bundle["chapters"]) == 3
    assert replay_service.EMBED_SLOT not in html
    assert replay_service.RENDERER_TAG not in html
    assert "var FiveeRenderer" in html


def test_the_cli_reports_the_written_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "requested-adventure.html"

    result = adventure_replay_sample.main(["--output", str(target)])

    assert result == 0
    assert target.is_file()
    output = capsys.readouterr().out
    assert str(target) in output
    assert "Chapters: 3" in output
    assert "Events:" in output
    assert "Format: adventure replay v1" in output
    assert "Modes: exploration -> combat -> exploration" in output


def test_the_adventure_showcase_has_a_console_command() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert pyproject["project"]["scripts"][
        "fivee-sim-adventure-replay-sample"
    ] == "fivee_sim.adventure_replay_sample:main"
