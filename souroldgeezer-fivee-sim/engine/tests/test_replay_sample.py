"""The checked-in animated replay showcase generator."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from fivee_sim import replay_sample
from fivee_sim.kernel.grid import TERRAIN
from fivee_sim.map_document import parse_document
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


def test_the_showcase_covers_each_animated_event_family() -> None:
    bundle = replay_sample.sample_bundle()

    assert bundle["format"] == "fivee-sim-replay"
    assert bundle["format_version"] == 2
    assert bundle["seed"] == 731204
    assert replay_service.validate_replay(bundle) == []
    assert parse_document(bundle["map"], source="sample", terrain=TERRAIN).name == (
        "Gatehouse Skirmish"
    )
    assert [event["kind"] for event in bundle["events"]] == [
        "move",
        "attack",
        "damage",
        "interact",
        "move",
        "attack",
        "damage",
        "move",
        "cast",
        "use_item",
        "heal",
        "interact",
        "move",
    ]
    assert bundle["map"]["features"] == [
        {
            "id": "inner-gate",
            "kind": "door",
            "at": [6, 4],
            "orientation": "horizontal",
            "state": "closed",
            "terrain": {"closed": "wall", "open": "floor"},
        },
        {
            "id": "east-stairs",
            "kind": "stairs_up",
            "at": [5, 4],
            "to_level": 1,
        },
    ]


def test_the_showcase_demonstrates_replay_v2_audit_and_state() -> None:
    bundle = replay_sample.sample_bundle()

    assert bundle["map"]["levels"][0]["name"] == "gallery"
    assert bundle["encounter"]["id"] == "sample-encounter"
    assert bundle["initial"]["combatants"]
    assert bundle["latest_state"] == bundle["checkpoints"][-1]["state"]
    assert {
        "conditions",
        "dodging",
        "reaction_available",
        "spell_slots",
        "items",
    } <= bundle["latest_state"]["combatants"][0].keys()
    assert bundle["latest_state"]["combatants"][1]["level"] == 1
    assert bundle["actions"]
    assert "Signal Flare" in bundle["content"]["records"]["spells"]
    assert "Field Restorative" in bundle["content"]["records"]["items"]
    assert {attempt["status"] for attempt in bundle["attempts"]} == {
        "refused",
        "success",
    }
    assert any(
        attempt["operation"] == "check"
        and attempt["arguments"]["skill"] == "Persuasion"
        for attempt in bundle["attempts"]
    )
    assert any(
        attempt["operation"] == "encounter_note"
        for attempt in bundle["attempts"]
    )
    assert bundle["integrity"]["algorithm"] == "sha256"


def test_the_showcase_writes_one_self_contained_html_file(tmp_path: Path) -> None:
    target = tmp_path / "showcase.html"

    result = replay_sample.write_sample(target)

    assert result == target
    html = target.read_text(encoding="utf-8")
    bundle = embedded_bundle(html)
    assert bundle["seed"] == 731204
    assert bundle["map"]["provenance"]["seed"] == 731204
    assert replay_service.EMBED_SLOT not in html
    assert replay_service.RENDERER_TAG not in html
    assert "var FiveeRenderer" in html


def test_the_cli_reports_the_written_path_and_seed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "requested-name.html"

    result = replay_sample.main(["--output", str(target)])

    assert result == 0
    assert target.is_file()
    output = capsys.readouterr().out
    assert str(target) in output
    assert "Seed: 731204" in output
    assert "Events: 13" in output
    assert "Format: replay v2" in output
    assert "Audit records: 3" in output
