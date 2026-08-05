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

#: The families the viewer animates, declared once and read by three checks —
#: ``tests/test_web_assets.py`` holds it against the viewer's own dispatch,
#: ``scripts/check-editor-behaviour.mjs`` proves the page really animates each
#: one, and the test below requires the showcase to put every one on screen. A
#: hard-coded list here was the defect: it pinned the sample against itself, so
#: a family the viewer gained could never turn it red.
ANIMATED_FAMILIES: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "animated-event-families.json").read_text(
        encoding="utf-8"
    )
)


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
    shown = {event["kind"] for event in bundle["events"]}
    missing = sorted(
        family["kind"] for family in ANIMATED_FAMILIES if family["kind"] not in shown
    )
    assert missing == [], (
        f"the showcase animates nothing for {missing}; its whole purpose is to put "
        "every animated family on screen"
    )
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


def test_the_showcase_state_agrees_with_the_fight_it_records() -> None:
    """The authored final state is reconciled against the authored event log.

    `_events()` and `_state_combatants()` are two hand-written fictions, and
    nothing else compares them: `validate_replay` hashes `latest_state` and the
    checkpoints but never folds the events to see whether they arrive there. So
    an edit to one half alone would leave a showcase whose checkpoint
    contradicts its own log, with every other test green — the same shape of
    unnoticed drift the hard-coded family list used to have.

    The expectation is derived from the events rather than restated, so it is
    the *relationship* being pinned, not a second copy of the constants.
    """
    bundle = replay_sample.sample_bundle()
    events = bundle["events"]
    final = {
        combatant["name"]: combatant for combatant in bundle["latest_state"]["combatants"]
    }

    for name, combatant in final.items():
        hp_changes = [
            event
            for event in events
            if event["kind"] in {"damage", "heal"} and event.get("target") == name
        ]
        if hp_changes:
            assert combatant["hp"] == hp_changes[-1]["data"]["hp"], (
                f"{name} ends on {combatant['hp']} hp, but the last hit-point event "
                f"in the log leaves them on {hp_changes[-1]['data']['hp']}"
            )

        saves = [
            event
            for event in events
            if event["kind"] == "death_save" and event.get("actor") == name
        ]
        expected_saves = (
            {
                "successes": saves[-1]["data"]["successes"],
                "failures": saves[-1]["data"]["failures"],
            }
            if saves
            else {"successes": 0, "failures": 0}
        )
        assert combatant["death_saves"] == expected_saves, (
            f"{name}'s recorded death saves disagree with the log's last "
            f"death_save event"
        )

        died = any(
            event["kind"] == "death" and event.get("actor") == name for event in events
        )
        assert combatant["dead"] is died, (
            f"{name} is dead={combatant['dead']} in the final state but the log "
            f"{'does' if died else 'does not'} record a death for them"
        )


def test_the_showcase_ends_in_a_recorded_party_victory() -> None:
    bundle = replay_sample.sample_bundle()

    assert bundle["latest_state"]["over"] is True
    assert bundle["latest_state"]["winner"] == "party"
    brute = next(
        combatant
        for combatant in bundle["latest_state"]["combatants"]
        if combatant["name"] == "Gatehouse Brute"
    )
    assert brute["hp"] == 0
    assert brute["conscious"] is False
    assert brute["dead"] is True
    assert brute["death_saves"] == {"successes": 0, "failures": 1}
    assert bundle["events"][-1]["kind"] == "death"
    assert bundle["events"][-1]["actor"] == "Gatehouse Brute"
    # The party's own casualty is recorded too: Arin was dropped, steadied and
    # brought back up, which is what leaves the fight with a `stabilised` and a
    # `down` in it at all.
    arin = next(
        combatant
        for combatant in bundle["latest_state"]["combatants"]
        if combatant["name"] == "Arin"
    )
    assert arin["conscious"] is True
    assert arin["hp"] == 8

    assert bundle["attempts"][-1]["operation"] == "encounter_note"
    assert bundle["attempts"][-1]["status"] == "success"
    assert bundle["attempts"][-1]["arguments"] == {
        "category": "outcome",
        "text": "Gatehouse secured. The party holds the inner gate.",
    }
    refused = next(
        attempt for attempt in bundle["attempts"] if attempt["status"] == "refused"
    )
    assert refused["index"] < bundle["attempts"][-1]["index"]
    # The refusal is "Mira cannot reach the gate from the gallery", so it has to
    # stamp after the move that put her there. Found by what it means rather
    # than by index: the scenario grows, and an index silently comes to name a
    # different event while the assertion goes on passing.
    reached_gallery = next(
        event
        for event in bundle["events"]
        if event["kind"] == "move" and event.get("data", {}).get("to_level") == 1
    )
    assert reached_gallery["timestamp"] <= refused["timestamp"]
    assert refused["timestamp"] < bundle["events"][-1]["timestamp"]
    assert bundle["events"][-1]["timestamp"] < bundle["attempts"][-1]["timestamp"]


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
    assert "Events: 33" in output
    assert "Format: replay v2" in output
    assert "Audit records: 4" in output
