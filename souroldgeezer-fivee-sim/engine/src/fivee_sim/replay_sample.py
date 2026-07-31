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
            {"id": "east-stairs", "kind": "stairs_down", "at": [9, 5]},
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
            detail="Restorative Word (slot 1).",
            round=1,
            turn="Mira",
            data={
                "spell": "Restorative Word",
                "slot_level": 1,
                "center": [25, 15],
                "targets": ["Arin"],
            },
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


def sample_bundle() -> dict[str, Any]:
    """Return a fresh replay bundle for the animated viewer showcase."""
    return replay_service.replay_bundle(
        name=_NAME,
        seed=SEED,
        map_payload=_map_payload(),
        initial_creatures=_initial_creatures(),
        map_open_features=[],
        events=_events(),
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
