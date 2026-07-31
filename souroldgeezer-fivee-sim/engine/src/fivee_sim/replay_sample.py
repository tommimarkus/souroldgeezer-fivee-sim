"""Generate a deterministic standalone replay that exercises the viewer.

The scenario is deliberately authored rather than simulated: its purpose is to
put every animated event family on screen in a short, repeatable sequence. The
bundle still goes through the same replay service and packaged viewer assets as
``replay_export(embed=True)``, so it is representative of the shipped format.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__
from .model.encounter import Event
from .service import replay as replay_service

__all__ = ["DEFAULT_OUTPUT", "SEED", "main", "sample_bundle", "write_sample"]

SEED = 731204
DEFAULT_OUTPUT = Path(".fivee-sim/replays/animated-replay-showcase.html")
_NAME = "Animated Replay Showcase"
_SOURCE = "Authored as 5E-compatible original content for the animated replay showcase"


def _map_payload() -> dict[str, Any]:
    return {
        "format": "fivee-sim-map",
        "format_version": 1,
        "name": "Gatehouse Skirmish",
        "grid": {"width": 12, "height": 8, "cell_feet": 5},
        "legend": {"#": "wall", ".": "floor"},
        "tiles": [
            "############",
            "#..........#",
            "#..........#",
            "#..........#",
            "#..........#",
            "#..........#",
            "#..........#",
            "############",
        ],
        "features": [
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
        ],
        "levels": [
            {
                "index": 1,
                "name": "gallery",
                "tiles": [
                    "############",
                    "#..........#",
                    "#..........#",
                    "#..........#",
                    "#..........#",
                    "#..........#",
                    "#..........#",
                    "############",
                ],
                "elevation": {"default": 10, "squares": []},
                "features": [
                    {
                        "id": "gallery-stairs",
                        "kind": "stairs_down",
                        "at": [5, 4],
                        "to_level": 0,
                    }
                ],
            }
        ],
        "provenance": {
            "generator": "animated-replay-showcase",
            "seed": SEED,
            "params": {},
            "edited": False,
            "source": _SOURCE,
        },
    }


def _initial_creatures() -> list[dict[str, Any]]:
    return [
        {
            "name": "Arin",
            "team": "party",
            "position": [10, 15],
            "hp": 20,
            "max_hp": 20,
        },
        {
            "name": "Mira",
            "team": "party",
            "position": [15, 25],
            "hp": 12,
            "max_hp": 12,
        },
        {
            "name": "Gatehouse Brute",
            "team": "monsters",
            "position": [45, 15],
            "hp": 18,
            "max_hp": 18,
        },
    ]


def _events() -> list[dict[str, Any]]:
    facts = [
        Event(
            kind="move",
            actor="Arin",
            detail="Arin advances toward the inner gate.",
            round=1,
            turn="Arin",
            data={"origin": [10, 15], "destination": [25, 15], "cost": 15},
        ),
        Event(
            kind="attack",
            actor="Arin",
            target="Gatehouse Brute",
            detail="Long Blade: 10 slashing damage.",
            round=1,
            turn="Arin",
            data={
                "attack": "Long Blade",
                "hit": True,
                "critical": False,
                "natural": 15,
                "total": 20,
                "advantage": "normal",
                "damage": 10,
                "cover": 0,
            },
        ),
        Event(
            kind="damage",
            target="Gatehouse Brute",
            detail="10 damage, 8/18 hit points left.",
            round=1,
            turn="Arin",
            data={"amount": 10, "hp": 8, "max_hp": 18},
        ),
        Event(
            kind="interact",
            actor="Arin",
            detail="Arin opens inner-gate.",
            round=1,
            turn="Arin",
            data={"feature": "inner-gate", "open": True},
        ),
        Event(
            kind="move",
            actor="Gatehouse Brute",
            detail="The gatehouse brute pushes through the opening.",
            round=1,
            turn="Gatehouse Brute",
            data={"origin": [45, 15], "destination": [35, 15], "cost": 10},
        ),
        Event(
            kind="attack",
            actor="Gatehouse Brute",
            target="Arin",
            detail="Heavy Mace: 13 bludgeoning damage.",
            round=1,
            turn="Gatehouse Brute",
            data={
                "attack": "Heavy Mace",
                "hit": True,
                "critical": False,
                "natural": 17,
                "total": 22,
                "advantage": "normal",
                "damage": 13,
                "cover": 0,
            },
        ),
        Event(
            kind="damage",
            target="Arin",
            detail="13 damage, 7/20 hit points left.",
            round=1,
            turn="Gatehouse Brute",
            data={"amount": 13, "hp": 7, "max_hp": 20},
        ),
        Event(
            kind="move",
            actor="Mira",
            detail="Mira moves into range of the wounded fighter.",
            round=1,
            turn="Mira",
            data={"origin": [15, 25], "destination": [25, 20], "cost": 10},
        ),
        Event(
            kind="cast",
            actor="Mira",
            target="Gatehouse Brute",
            detail="Signal Flare (slot 1).",
            round=1,
            turn="Mira",
            data={
                "spell": "Signal Flare",
                "slot_level": 1,
                "targets": ["Gatehouse Brute"],
            },
        ),
        Event(
            kind="use_item",
            actor="Mira",
            target="Arin",
            detail="Mira uses Field Restorative on Arin.",
            round=1,
            turn="Mira",
            data={"item": "Field Restorative", "quantity": 0},
        ),
        Event(
            kind="heal",
            target="Arin",
            detail="8 hit points restored, 15/20.",
            round=1,
            turn="Mira",
            data={"amount": 8, "hp": 15, "max_hp": 20},
        ),
        Event(
            kind="interact",
            actor="Mira",
            detail="Mira closes inner-gate.",
            round=1,
            turn="Mira",
            data={"feature": "inner-gate", "open": False},
        ),
        Event(
            kind="move",
            actor="Mira",
            detail="(25, 20) [level 0] -> (25, 20) [level 1] (20 ft used)",
            round=1,
            turn="Mira",
            data={
                "origin": [25, 20],
                "planned_destination": [25, 20],
                "destination": [25, 20],
                "cost": 20,
                "from_level": 0,
                "planned_to_level": 1,
                "to_level": 1,
                "completed": True,
            },
        ),
    ]
    return [
        Event(
            kind=event.kind,
            actor=event.actor,
            target=event.target,
            detail=event.detail,
            seq=seq,
            round=event.round,
            turn=event.turn,
            data=event.data,
        ).as_dict()
        for seq, event in enumerate(facts)
    ]


def _normalized_combatants() -> list[dict[str, Any]]:
    common: dict[str, Any] = {
        "speed": 30,
        "size": "medium",
        "abilities": {
            "strength": 10,
            "dexterity": 10,
            "constitution": 10,
            "intelligence": 10,
            "wisdom": 10,
            "charisma": 10,
        },
        "save_bonuses": {},
        "attacks_per_action": 1,
        "pack_tactics": False,
        "undead_fortitude": False,
        "spells": [],
        "spell_slots": {},
        "spell_save_dc": 10,
        "spell_attack_bonus": 0,
        "resistances": [],
        "immunities": [],
        "vulnerabilities": [],
        "items": {},
        "conditions": [],
        "level": 0,
        "provenance": _SOURCE,
    }
    return [
        {
            **common,
            "name": "Arin",
            "team": "party",
            "ac": 17,
            "max_hp": 20,
            "hp": 20,
            "abilities": {**common["abilities"], "strength": 16, "charisma": 14},
            "attacks": [
                {
                    "name": "Long Blade",
                    "attack_bonus": 5,
                    "damage": "1d8+3",
                    "damage_type": "slashing",
                    "kind": "melee",
                }
            ],
            "position": [10, 15],
        },
        {
            **common,
            "name": "Mira",
            "team": "party",
            "ac": 14,
            "max_hp": 12,
            "hp": 12,
            "abilities": {**common["abilities"], "wisdom": 16, "charisma": 16},
            "attacks": [],
            "spells": ["Signal Flare"],
            "spell_slots": {"1": 2},
            "spell_save_dc": 13,
            "spell_attack_bonus": 5,
            "items": {"Field Restorative": 1},
            "position": [15, 25],
        },
        {
            **common,
            "name": "Gatehouse Brute",
            "team": "monsters",
            "ac": 15,
            "max_hp": 18,
            "hp": 18,
            "abilities": {**common["abilities"], "strength": 16, "constitution": 14},
            "attacks": [
                {
                    "name": "Heavy Mace",
                    "attack_bonus": 5,
                    "damage": "1d8+3",
                    "damage_type": "bludgeoning",
                    "kind": "melee",
                }
            ],
            "position": [45, 15],
        },
    ]


def _state_combatants(*, final: bool) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = [
        {
            "name": "Arin",
            "team": "party",
            "position": [25, 15] if final else [10, 15],
            "hp": 15 if final else 20,
            "max_hp": 20,
            "ac": 17,
            "level": 0,
            "conditions": [],
            "dodging": False,
            "concentrating_on": None,
            "reaction_available": False if final else True,
            "disengaged": False,
            "conscious": True,
            "dead": False,
            "stable": False,
            "death_saves": {"successes": 0, "failures": 0},
            "spell_slots": {},
            "items": {"Potion": 1},
        },
        {
            "name": "Mira",
            "team": "party",
            "position": [25, 20] if final else [15, 25],
            "hp": 12,
            "max_hp": 12,
            "ac": 14,
            "level": 1 if final else 0,
            "conditions": [],
            "concentrating_on": None,
            "dodging": False,
            "reaction_available": True,
            "disengaged": False,
            "conscious": True,
            "dead": False,
            "stable": False,
            "death_saves": {"successes": 0, "failures": 0},
            "spell_slots": {"1": 1 if final else 2},
            "items": {"Field Restorative": 0 if final else 1},
        },
        {
            "name": "Gatehouse Brute",
            "team": "monsters",
            "position": [35, 15] if final else [45, 15],
            "hp": 8 if final else 18,
            "max_hp": 18,
            "ac": 15,
            "level": 0,
            "conditions": [],
            "dodging": False,
            "concentrating_on": None,
            "reaction_available": True,
            "disengaged": False,
            "conscious": True,
            "dead": False,
            "stable": False,
            "death_saves": {"successes": 0, "failures": 0},
            "spell_slots": {},
            "items": {},
        },
    ]
    return facts


def _state(*, final: bool) -> dict[str, Any]:
    return {
        "round": 1,
        "turn": "Mira" if final else "Arin",
        "order": ["Arin", "Gatehouse Brute", "Mira"],
        "over": False,
        "winner": None,
        "movement_rule": "5-5-5",
        "movement_left": 0 if final else 30,
        "action_available": not final,
        "bonus_action_available": not final,
        "ongoing_effects": [],
        "combatants": _state_combatants(final=final),
        "map": {
            "open_features": [],
            "features": {"inner-gate": {"open": False}},
        },
        "map_source": {
            "kind": "captured",
            "name": "Gatehouse Skirmish",
            "stale": False,
        },
    }


def _actions() -> list[dict[str, Any]]:
    return [
        {
            "index": index,
            "round": 1,
            "actor": actor,
            "action": action,
            "first_event": first,
            "event_count": count,
        }
        for index, (actor, action, first, count) in enumerate(
            [
                ("Arin", {"kind": "move", "to_position": [25, 15]}, 0, 1),
                ("Arin", {"kind": "attack", "target": "Gatehouse Brute"}, 1, 2),
                ("Arin", {"kind": "interact", "feature": "inner-gate"}, 3, 1),
                ("Gatehouse Brute", {"kind": "move", "to_position": [35, 15]}, 4, 1),
                ("Gatehouse Brute", {"kind": "attack", "target": "Arin"}, 5, 2),
                ("Mira", {"kind": "move", "to_position": [25, 20]}, 7, 1),
                ("Mira", {"kind": "cast", "spell": "Signal Flare"}, 8, 1),
                (
                    "Mira",
                    {
                        "kind": "use_item",
                        "item": "Field Restorative",
                        "target": "Arin",
                    },
                    9,
                    2,
                ),
                ("Mira", {"kind": "interact", "feature": "inner-gate"}, 11, 1),
                (
                    "Mira",
                    {"kind": "move", "to_position": [25, 20], "to_level": 1},
                    12,
                    1,
                ),
            ]
        )
    ]


def _attempts() -> list[dict[str, Any]]:
    return [
        {
            "index": 0,
            "timestamp": "2026-08-01T12:00:00Z",
            "started_at": "2026-08-01T11:59:59Z",
            "operation": "check",
            "request_id": "sample-persuasion",
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
            "timestamp": "2026-08-01T12:00:01Z",
            "started_at": "2026-08-01T12:00:01Z",
            "operation": "encounter_note",
            "request_id": "sample-note",
            "arguments": {
                "category": "playtest",
                "text": "The successful Persuasion check buys one round before combat.",
            },
            "status": "success",
            "result": {"category": "playtest"},
        },
        {
            "index": 2,
            "timestamp": "2026-08-01T12:00:15Z",
            "started_at": "2026-08-01T12:00:15Z",
            "operation": "encounter_act",
            "request_id": "sample-refusal",
            "arguments": {"kind": "interact", "feature": "inner-gate"},
            "status": "refused",
            "error": "Mira cannot reach inner-gate from gallery level 1",
        },
    ]


def sample_bundle() -> dict[str, Any]:
    """Return a fresh, valid replay-v2 bundle for the viewer showcase."""
    initial_state = _state(final=False)
    latest_state = _state(final=True)
    events = _events()
    timestamps = [f"2026-08-01T12:00:{index + 2:02d}Z" for index in range(len(events))]
    checkpoints = [
        {
            "index": 0,
            "timestamp": "2026-08-01T11:59:58Z",
            "event_count": 0,
            "state_hash": replay_service.canonical_sha256(initial_state),
            "state": initial_state,
        },
        {
            "index": 1,
            "timestamp": "2026-08-01T12:00:14Z",
            "event_count": len(events),
            "state_hash": replay_service.canonical_sha256(latest_state),
            "state": latest_state,
        },
    ]
    return replay_service.replay_bundle_v2(
        name=_NAME,
        engine_version=__version__,
        encounter_id="sample-encounter",
        seed=SEED,
        movement_rule="5-5-5",
        map_payload=_map_payload(),
        initial_creatures=_initial_creatures(),
        normalized_combatants=_normalized_combatants(),
        initial_state=initial_state,
        map_open_features=[],
        actions=_actions(),
        events=events,
        event_timestamps=timestamps,
        latest_state=latest_state,
        checkpoints=checkpoints,
        attempts=_attempts(),
        content_snapshot={
            "builtin": "exclude",
            "packs": [
                {
                    "label": "animated-replay-showcase",
                    "level": "project",
                    "pack": "animated-replay-showcase",
                    "version": "1.0",
                    "provenance": _SOURCE,
                    "path": "",
                    "counts": {"items": 1, "spells": 1, "terrain": 2},
                }
            ],
            "retained_conditions": [],
            "records": {
                "conditions": {},
                "creatures": {},
                "items": {
                    "Field Restorative": {
                        "record": {
                            "name": "Field Restorative",
                            "description": "A quick tonic for the gatehouse patrol.",
                            "use": {"heal": "1d8"},
                            "provenance": _SOURCE,
                            "unmodelled": [],
                        },
                        "source": _SOURCE,
                    }
                },
                "spells": {
                    "Signal Flare": {
                        "record": {
                            "name": "Signal Flare",
                            "level": 1,
                            "school": "Evocation",
                            "range_feet": 60,
                            "provenance": _SOURCE,
                            "unmodelled": [],
                        },
                        "source": _SOURCE,
                    }
                },
                "terrain": {
                    "floor": {
                        "record": {
                            "name": "floor",
                            "effects": {
                                "move_cost_multiplier": 1,
                                "passable": True,
                                "opaque": False,
                                "cover": 0,
                            },
                            "provenance": _SOURCE,
                            "unmodelled": [],
                        },
                        "source": _SOURCE,
                    },
                    "wall": {
                        "record": {
                            "name": "wall",
                            "effects": {
                                "move_cost_multiplier": 1,
                                "passable": False,
                                "opaque": True,
                                "cover": 3,
                            },
                            "provenance": _SOURCE,
                            "unmodelled": [],
                        },
                        "source": _SOURCE,
                    },
                },
            },
            "provenance": _SOURCE,
        },
    )


def write_sample(output: str | Path = DEFAULT_OUTPUT) -> Path:
    """Write the self-contained showcase HTML and return its path."""
    static = resources.files("fivee_sim.editor") / "static"
    viewer = (static / "viewer.html").read_text(encoding="utf-8")
    renderer = (static / "renderer.js").read_text(encoding="utf-8")
    bundle_json = replay_service.serialize_bundle(sample_bundle())
    html = replay_service.embed_in_viewer(viewer, bundle_json, renderer_js=renderer)

    target = Path(output).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    """Generate the showcase from a shell or console-script entry point."""
    parser = argparse.ArgumentParser(
        prog="fivee-sim-replay-sample",
        description="Generate a standalone replay that exercises every viewer animation.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"HTML path to write (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args(argv)

    target = write_sample(args.output)
    print(f"Replay: {target}")
    print(f"Seed: {SEED}")
    print(f"Events: {len(_events())}")
    print("Format: replay v2")
    print(f"Audit records: {len(_attempts())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
