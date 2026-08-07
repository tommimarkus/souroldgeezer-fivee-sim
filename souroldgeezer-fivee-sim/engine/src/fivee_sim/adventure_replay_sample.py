"""Generate a deterministic three-chapter adventure replay showcase.

The existing :mod:`fivee_sim.replay_sample` owns breadth inside one fight: it
puts every animated event family on screen.  This module owns the other viewer
contract — a run moves from exploration into that fight and on into aftermath,
carrying the party across both chapter boundaries.  Both are authored samples,
and both use the shipped replay composer and viewer embedding primitives.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from copy import deepcopy
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__, replay_sample
from .model.encounter import EncounterMode, Event
from .service import adventures
from .service import replay as replay_service

__all__ = ["DEFAULT_OUTPUT", "main", "sample_bundle", "write_sample"]

DEFAULT_OUTPUT = Path(".fivee-sim/replays/adventure-replay-showcase.html")
_ADVENTURE_ID = "adventure-showcase"
_ADVENTURE_NAME = "The Gatehouse Run"
_PARTY = ("Arin", "Mira")


def _combatant(state: dict[str, Any], name: str) -> dict[str, Any]:
    return next(one for one in state["combatants"] if one["name"] == name)


def _exploration_state(source: dict[str, Any]) -> dict[str, Any]:
    """Return the same battlefield state without a fight holding the floor."""
    state = deepcopy(source)
    state.update(
        {
            "round": 1,
            "turn": None,
            "over": False,
            "winner": None,
            "movement_left": 30,
            "action_available": True,
            "bonus_action_available": True,
        }
    )
    return state


def _event(
    seq: int,
    kind: str,
    *,
    actor: str = "",
    target: str = "",
    detail: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return Event(
        kind=kind,
        actor=actor,
        target=target,
        detail=detail,
        seq=seq,
        round=1,
        turn=actor,
        data=data or {},
    ).as_dict()


def _initial_records(
    combat: dict[str, Any], state: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Overlay one phase's opening state on the combat showcase's cast."""
    creatures = deepcopy(combat["initial"]["creatures"])
    normalized = deepcopy(combat["initial"]["combatants"])
    by_name = {one["name"]: one for one in state["combatants"]}
    for records in (creatures, normalized):
        for record in records:
            latest = by_name[record["name"]]
            for key in adventures.CARRIED_STATE_KEYS:
                if key in latest:
                    record[key] = deepcopy(latest[key])
    return creatures, normalized


def _phase_bundle(
    *,
    combat: dict[str, Any],
    name: str,
    encounter_id: str,
    seed: int,
    initial_state: dict[str, Any],
    latest_state: dict[str, Any],
    events: list[dict[str, Any]],
    event_timestamps: list[str],
    actions: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    creatures, normalized = _initial_records(combat, initial_state)
    checkpoints = [
        {
            "index": 0,
            "timestamp": event_timestamps[0],
            "event_count": 0,
            "state_hash": replay_service.canonical_sha256(initial_state),
            "state": initial_state,
        },
        {
            "index": 1,
            "timestamp": event_timestamps[-1],
            "event_count": len(events),
            "state_hash": replay_service.canonical_sha256(latest_state),
            "state": latest_state,
        },
    ]
    content = {
        key: deepcopy(value)
        for key, value in combat["content"].items()
        if key != "sha256"
    }
    return replay_service.replay_bundle_v2(
        name=name,
        engine_version=__version__,
        encounter_id=encounter_id,
        seed=seed,
        movement_rule=combat["encounter"]["movement_rule"],
        mode=EncounterMode.EXPLORATION.value,
        map_payload=combat["map"],
        initial_creatures=creatures,
        normalized_combatants=normalized,
        initial_state=initial_state,
        map_open_features=initial_state["map"]["open_features"],
        actions=actions,
        events=events,
        event_timestamps=event_timestamps,
        latest_state=latest_state,
        checkpoints=checkpoints,
        attempts=attempts,
        content_snapshot=content,
    )


def _arrival_bundle(combat: dict[str, Any]) -> dict[str, Any]:
    latest = _exploration_state(combat["initial"]["state"])
    initial = deepcopy(latest)
    _combatant(initial, "Arin")["position"] = [0, 15]
    _combatant(initial, "Mira")["position"] = [5, 25]
    events = [
        _event(
            0,
            "arrival",
            actor="Arin",
            detail="Arin reaches the gatehouse approach.",
            data={"position": [0, 15]},
        ),
        _event(
            1,
            "arrival",
            actor="Mira",
            detail="Mira follows along the southern wall.",
            data={"position": [5, 25]},
        ),
        _event(
            2,
            "move",
            actor="Arin",
            detail="Arin advances under the gatehouse windows.",
            data={"origin": [0, 15], "destination": [5, 15], "cost": 5},
        ),
        _event(
            3,
            "move",
            actor="Mira",
            detail="Mira keeps pace behind cover.",
            data={"origin": [5, 25], "destination": [10, 25], "cost": 5},
        ),
        _event(
            4,
            "move",
            actor="Arin",
            detail="Arin takes the position where the parley will begin.",
            data={"origin": [5, 15], "destination": [10, 15], "cost": 5},
        ),
        _event(
            5,
            "move",
            actor="Mira",
            detail="Mira reaches the inner approach and signals readiness.",
            data={"origin": [10, 25], "destination": [15, 25], "cost": 5},
        ),
    ]
    actions = [
        {
            "index": index,
            "round": 1,
            "actor": event["actor"],
            "action": {
                "kind": "move",
                "to_position": event["data"]["destination"],
            },
            "first_event": index + 2,
            "event_count": 1,
        }
        for index, event in enumerate(events[2:])
    ]
    attempts = [
        {
            "index": 0,
            "timestamp": "2026-08-01T11:58:05Z",
            "started_at": "2026-08-01T11:58:04Z",
            "operation": "check",
            "request_id": "adventure-arrival-check",
            "arguments": {
                "ability": "charisma",
                "skill": "Persuasion",
                "modifier": 5,
                "dc": 14,
            },
            "status": "success",
            "result": {
                "natural": 12,
                "total": 17,
                "dc": 14,
                "success": True,
                "detail": "d20 [12] +5 = 17 vs DC 14: success",
            },
        },
        {
            "index": 1,
            "timestamp": "2026-08-01T11:58:06Z",
            "started_at": "2026-08-01T11:58:06Z",
            "operation": "encounter_note",
            "request_id": "adventure-arrival-note",
            "arguments": {
                "category": "dialogue",
                "speaker": "Mira",
                "text": "The signal bought us a moment. Stay ready.",
            },
            "status": "success",
            "result": {"category": "dialogue", "speaker": "Mira"},
        },
    ]
    timestamps = [f"2026-08-01T11:58:{index:02d}Z" for index in range(len(events))]
    return _phase_bundle(
        combat=combat,
        name="Arrival at the Gatehouse",
        encounter_id="adventure-arrival",
        seed=731203,
        initial_state=initial,
        latest_state=latest,
        events=events,
        event_timestamps=timestamps,
        actions=actions,
        attempts=attempts,
    )


def _aftermath_bundle(combat: dict[str, Any]) -> dict[str, Any]:
    initial = _exploration_state(combat["latest_state"])
    _combatant(initial, "Arin")["hp"] = 12
    latest = deepcopy(initial)
    arin = _combatant(latest, "Arin")
    arin.update({"hp": 16, "position": [30, 20], "facing": "east"})
    arin["items"]["Potion"] = 0
    mira = _combatant(latest, "Mira")
    mira.update({"position": [30, 25], "level": 0, "facing": "east"})
    latest["map"]["open_features"] = ["inner-gate"]
    latest["map"]["features"]["inner-gate"]["open"] = True
    events = [
        _event(
            0,
            "use_item",
            actor="Arin",
            target="Arin",
            detail="Arin drinks the remaining restorative.",
            data={"item": "Potion", "quantity": 0},
        ),
        _event(
            1,
            "heal",
            target="Arin",
            detail="4 hit points restored, 16/20.",
            data={"amount": 4, "hp": 16, "max_hp": 20},
        ),
        _event(
            2,
            "move",
            actor="Mira",
            detail="Mira descends from the gallery.",
            data={
                "origin": [25, 20],
                "destination": [25, 20],
                "cost": 10,
                "to_level": 0,
            },
        ),
        _event(
            3,
            "move",
            actor="Arin",
            detail="Arin crosses to the captured gate.",
            data={"origin": [25, 15], "destination": [30, 20], "cost": 10},
        ),
        _event(
            4,
            "interact",
            actor="Arin",
            detail="Arin opens inner-gate for the relief party.",
            data={"feature": "inner-gate", "open": True},
        ),
        _event(
            5,
            "move",
            actor="Mira",
            detail="Mira joins Arin beside the secured entrance.",
            data={"origin": [25, 20], "destination": [30, 25], "cost": 10},
        ),
    ]
    actions = [
        {
            "index": 0,
            "round": 1,
            "actor": "Arin",
            "action": {"kind": "use_item", "item": "Potion", "target": "Arin"},
            "first_event": 0,
            "event_count": 2,
        },
        {
            "index": 1,
            "round": 1,
            "actor": "Mira",
            "action": {"kind": "move", "to_level": 0, "to_position": [25, 20]},
            "first_event": 2,
            "event_count": 1,
        },
        {
            "index": 2,
            "round": 1,
            "actor": "Arin",
            "action": {"kind": "move", "to_position": [30, 20]},
            "first_event": 3,
            "event_count": 1,
        },
        {
            "index": 3,
            "round": 1,
            "actor": "Arin",
            "action": {"kind": "interact", "feature": "inner-gate"},
            "first_event": 4,
            "event_count": 1,
        },
        {
            "index": 4,
            "round": 1,
            "actor": "Mira",
            "action": {"kind": "move", "to_position": [30, 25]},
            "first_event": 5,
            "event_count": 1,
        },
    ]
    attempts = [
        {
            "index": 0,
            "timestamp": "2026-08-01T12:02:04Z",
            "started_at": "2026-08-01T12:02:04Z",
            "operation": "encounter_note",
            "request_id": "adventure-aftermath-note",
            "arguments": {
                "category": "dialogue",
                "speaker": "Mira",
                "text": "The gallery is clear. Bring the relief party through.",
            },
            "status": "success",
            "result": {"category": "dialogue", "speaker": "Mira"},
        },
        {
            "index": 1,
            "timestamp": "2026-08-01T12:02:06Z",
            "started_at": "2026-08-01T12:02:06Z",
            "operation": "encounter_note",
            "request_id": "adventure-aftermath-outcome",
            "arguments": {
                "category": "outcome",
                "text": "Gatehouse secured. The road through the inner gate is open.",
            },
            "status": "success",
            "result": {"category": "outcome"},
        },
    ]
    timestamps = [f"2026-08-01T12:02:{index:02d}Z" for index in range(len(events))]
    return _phase_bundle(
        combat=combat,
        name="Aftermath at the Gatehouse",
        encounter_id="adventure-aftermath",
        seed=731205,
        initial_state=initial,
        latest_state=latest,
        events=events,
        event_timestamps=timestamps,
        actions=actions,
        attempts=attempts,
    )


def sample_bundle() -> dict[str, Any]:
    """Return a fresh, valid exploration-combat-aftermath replay envelope."""
    combat = replay_sample.sample_bundle()
    arrival = _arrival_bundle(combat)
    aftermath = _aftermath_bundle(combat)
    chapters = [
        {
            "index": 0,
            "encounter_id": arrival["encounter"]["id"],
            "linked_at": "2026-08-01T11:57:59Z",
            "carried": [],
            "mode": EncounterMode.EXPLORATION.value,
            "replay": arrival,
        },
        {
            "index": 1,
            "encounter_id": combat["encounter"]["id"],
            "linked_at": "2026-08-01T11:59:59Z",
            "carried": list(_PARTY),
            "mode": EncounterMode.COMBAT.value,
            "replay": combat,
        },
        {
            "index": 2,
            "encounter_id": aftermath["encounter"]["id"],
            "linked_at": "2026-08-01T12:01:59Z",
            "carried": list(_PARTY),
            "mode": EncounterMode.EXPLORATION.value,
            "recovery": {"Arin": {"hp": 12}},
            "recovery_note": "Short rest after the gatehouse battle",
            "replay": aftermath,
        },
    ]
    return replay_service.adventure_replay_bundle(
        engine_version=__version__,
        adventure={
            "id": _ADVENTURE_ID,
            "name": _ADVENTURE_NAME,
            "created_at": "2026-08-01T11:57:58Z",
            "status": "finalized",
        },
        chapters=chapters,
    )


def write_sample(output: str | Path = DEFAULT_OUTPUT) -> Path:
    """Write the self-contained adventure showcase HTML and return its path."""
    static = resources.files("fivee_sim.web") / "static"
    viewer = (static / "viewer.html").read_text(encoding="utf-8")
    renderer = (static / "renderer.js").read_text(encoding="utf-8")
    bundle_json = replay_service.serialize_bundle(sample_bundle())
    html = replay_service.embed_in_viewer(viewer, bundle_json, renderer_js=renderer)

    target = Path(output).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    """Generate the adventure showcase from a shell or console entry point."""
    parser = argparse.ArgumentParser(
        prog="fivee-sim-adventure-replay-sample",
        description="Generate a three-chapter standalone adventure replay.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"HTML path to write (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args(argv)

    bundle = sample_bundle()
    target = write_sample(args.output)
    modes = " -> ".join(chapter["mode"] for chapter in bundle["chapters"])
    events = sum(len(chapter["replay"]["events"]) for chapter in bundle["chapters"])
    print(f"Adventure: {target}")
    print(f"Chapters: {len(bundle['chapters'])}")
    print(f"Events: {events}")
    print("Format: adventure replay v1")
    print(f"Modes: {modes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
