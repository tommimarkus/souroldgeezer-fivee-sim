"""One map, two readers, and nobody writing to it.

``Encounter`` answers "what is this square" from its own flattened claim index
(``_feature_squares``, resolved against ``map_state.open_features``).
:class:`~fivee_sim.service.maps.ResolvedLevel` answers the same question from
the same storey and the same open set. They are two derivations of one thing,
and the only reason they agree today is that both were written from
:meth:`~fivee_sim.map_types.MapFeatureRecord.claims` — a shared origin, not a
shared implementation. This file holds them against each other square by square,
over a corpus wide enough that a divergence has somewhere to show: storeys with
connectors, sight links and an authored light, an overlay fixture that moves
terrain *and* ground height, and a linked door pair — each in both of the states
its fixtures can be authored in.

Cover is checked alongside terrain and height because it is a genuine second
opinion: ``Encounter._cover_of`` is a pure function of the resolved terrain
kind, so deriving it from ``ResolvedLevel.terrain_at`` proves the fight's cover
reads the fixture-resolved square rather than the raw plane underneath it.
Illumination deliberately is not: ``Encounter._illumination_at`` asks about a
*creature* — ambient light, authored lights, and where it stands —
``ResolvedLevel`` models none of that, and there is no second answer to hold it
against.

The fixture-name assertion is the other half. Which document features become
fixtures a fight owns is decided by one line of :meth:`MapLevel.fixtures`: a
feature with no ``state`` is skipped, so a spawn hint, a drawn stairway and a
brazier stay document-level. That gate used to be applied *structurally* — by
``to_grid``, which built a battle map that had no slot for such a feature at all
— so no consumer could read one by accident. It is a method call now, made at
nine or so sites, and this is where the whole set of them is held to it.

Two more properties are pinned here because nothing else pinned them and both
are load-bearing for "the map is a static artifact": a :class:`MapDocument` is
frozen, and a fight that operates a door records the change in
``map_state.open_features`` while leaving the document byte-for-byte as it found
it.
"""

from __future__ import annotations

import dataclasses
from copy import deepcopy
from random import Random
from typing import Any

import pytest

from fivee_sim.kernel.grid import (
    FEET_PER_SQUARE,
    TERRAIN,
    CoverGrade,
    Square,
    terrain_effect_of,
)
from fivee_sim.map_document import parse_document
from fivee_sim.map_types import MapDocument
from fivee_sim.model.encounter import Action, ActionKind, Encounter
from fivee_sim.service.maps import ResolvedLevel

from .conftest import advance_to, fighter

FIXTURE = "Authored for the test suite; 5E-compatible original content"


def chamber_payload() -> dict[str, Any]:
    """A single storey: one door that can swing, one spawn hint that cannot.

    The spawn hint is the whole point of including it — it carries no ``state``,
    so it must never appear among the fight's fixtures.
    """
    return {
        "format": "fivee-sim-map",
        "format_version": 1,
        "name": "parity-chamber",
        "grid": {"width": 6, "height": 5, "cell_feet": 5},
        "legend": {".": "floor", "#": "wall", "%": "difficult"},
        "tiles": [
            "######",
            "#....#",
            "#.%..#",
            "#....#",
            "###.##",
        ],
        "features": [
            {
                "id": "chamber-door",
                "kind": "door",
                "at": [3, 4],
                "orientation": "horizontal",
                "state": "closed",
            },
            {"id": "spawn-party", "kind": "spawn", "at": [1, 1], "team": "party"},
        ],
        "provenance": {
            "generator": "hand",
            "seed": 7,
            "params": {"width": 6, "height": 5},
            "edited": False,
            "source": FIXTURE,
        },
    }


def storeyed_payload() -> dict[str, Any]:
    """The chamber with a gallery over it: connectors, sight links, and a light.

    The stairway pair and the brazier are all stateless, so this map also says
    that three annotations of three different kinds — one that becomes a
    connector, one that becomes a sight link, one that becomes a light source —
    still become no fixture at all.
    """
    raw = chamber_payload()
    raw["name"] = "parity-gallery"
    raw["features"].append(
        {
            "id": "stair-foot",
            "kind": "stairs_up",
            "at": [3, 3],
            "to_level": 1,
            "sight_to_levels": [1],
        }
    )
    raw["features"].append(
        {
            "id": "brazier",
            "kind": "light",
            "at": [4, 1],
            "light": {"bright": 20, "dim": 20, "color": "#ffcc88"},
        }
    )
    raw["levels"] = [
        {
            "index": 1,
            "name": "gallery",
            "tiles": ["######", "#....#", "#....#", "#....#", "######"],
            "elevation": {"default": 10, "squares": [[2, 2, 15]]},
            "features": [
                {
                    "id": "stair-head",
                    "kind": "stairs_down",
                    "at": [3, 3],
                    "to_level": 0,
                    "sight_to_levels": [0],
                },
                {
                    "id": "gallery-hatch",
                    "kind": "hatch",
                    "at": [1, 3],
                    "state": "closed",
                    "terrain": {"closed": "wall", "open": "floor"},
                },
            ],
        }
    ]
    return raw


def sluice_payload() -> dict[str, Any]:
    """A gate whose overlay floods the far room and drops it five feet.

    The tiles say floor either way; only the fixture record says otherwise, so
    a reader that consults the plane instead of the claim gets this map wrong in
    both layers at once.
    """
    raw = chamber_payload()
    raw["name"] = "parity-sluice"
    raw["grid"] = {"width": 7, "height": 6, "cell_feet": 5}
    raw["tiles"] = [
        "#######",
        "#..#..#",
        "#..#..#",
        "#..#..#",
        "#.....#",
        "#######",
    ]
    raw["features"] = [
        {
            "id": "sluice gate",
            "kind": "door",
            "at": [3, 2],
            "orientation": "vertical",
            "state": "closed",
            "affects": [
                {
                    "cells": [[4, 1], [5, 1], [4, 2], [5, 2], [4, 3], [5, 3]],
                    "terrain": {"closed": "floor", "open": "water"},
                    "elevation": {"closed": 0, "open": -5},
                }
            ],
        },
        {"id": "spawn-party", "kind": "spawn", "at": [1, 1], "team": "party"},
    ]
    raw["provenance"]["params"] = {"width": 7, "height": 6}
    return raw


def linked_payload() -> dict[str, Any]:
    """A reciprocal pair of doors, which one operation moves together."""
    raw = chamber_payload()
    raw["name"] = "parity-double-doors"
    raw["features"] = [
        {
            "id": "door-left",
            "kind": "door",
            "at": [2, 4],
            "orientation": "horizontal",
            "hinge": "west",
            "swing": "north",
            "state": "closed",
            "linked_to": "door-right",
        },
        {
            "id": "door-right",
            "kind": "door",
            "at": [3, 4],
            "orientation": "horizontal",
            "hinge": "east",
            "swing": "north",
            "state": "closed",
            "linked_to": "door-left",
        },
        {"id": "spawn-party", "kind": "spawn", "at": [1, 1], "team": "party"},
    ]
    return raw


def in_state(payload: dict[str, Any], state: str) -> dict[str, Any]:
    """Every fixture in ``payload`` authored ``state``; annotations untouched.

    Only an entry that already declares a ``state`` is rewritten, so the
    document's own fixture/annotation split survives the transformation — a
    spawn hint does not acquire a state by being asked for the open variant.
    """
    raw = deepcopy(payload)
    levels: list[Any] = raw.get("levels", [])
    for entries in [raw["features"], *(level["features"] for level in levels)]:
        for entry in entries:
            if "state" in entry:
                entry["state"] = state
    return raw


def standing_at(square: Square) -> tuple[int, int]:
    """A creature position, in feet, for the square it should stand in.

    ``Creature.position`` is feet and always has been; ``_adopt_map`` snaps to
    the square's centre. Writing squares in the corpus and converting here keeps
    the placements readable against the tiles above them.
    """
    return (square[0] * FEET_PER_SQUARE, square[1] * FEET_PER_SQUARE)


@dataclasses.dataclass(frozen=True)
class MapCase:
    """One corpus map in one fixture state, plus two squares to stand on."""

    name: str
    payload: dict[str, Any]
    party: Square
    foes: Square


#: Each map with two floor squares nothing in it ever turns into a wall or
#: water, so the same placement is legal in either state.
CORPUS: tuple[tuple[str, Any, Square, Square], ...] = (
    ("chamber", chamber_payload, (1, 1), (4, 3)),
    ("gallery", storeyed_payload, (1, 1), (4, 3)),
    ("sluice", sluice_payload, (1, 1), (1, 4)),
    ("double-doors", linked_payload, (1, 1), (4, 3)),
)


def corpus() -> list[MapCase]:
    return [
        MapCase(f"{name}-{state}", in_state(build(), state), party, foes)
        for name, build, party, foes in CORPUS
        for state in ("closed", "open")
    ]


def encounter_on(case: MapCase) -> tuple[Encounter, MapDocument]:
    document = parse_document(case.payload, source=case.name, terrain=TERRAIN)
    encounter = Encounter(
        [
            fighter(position=standing_at(case.party)),
            fighter("Grull", team="foes", position=standing_at(case.foes)),
        ],
        Random(1),
        map_document=document,
    )
    return encounter, document


def expected_cover(kind: str) -> int:
    effect = terrain_effect_of(kind, TERRAIN)
    return int(CoverGrade.TOTAL) if effect.opaque else effect.cover


class TestTwoReadersAgree:
    @pytest.mark.parametrize("case", corpus(), ids=lambda case: case.name)
    def test_every_square_of_every_storey_resolves_identically(
        self, case: MapCase
    ) -> None:
        encounter, document = encounter_on(case)
        assert encounter.map_state is not None
        open_features = encounter.map_state.open_features

        divergent: list[str] = []
        for level in sorted(document.levels):
            resolved = ResolvedLevel.of(
                document.levels[level], document.legend, open_features
            )
            for y in range(document.grid.height):
                for x in range(document.grid.width):
                    square = (x, y)
                    fight = (
                        encounter._terrain_at_level(level, square),
                        encounter._elevation_at(level, square),
                        encounter._cover_of(level, square),
                    )
                    reader = (
                        resolved.terrain_at(square),
                        resolved.height_at(square),
                        expected_cover(resolved.terrain_at(square)),
                    )
                    if fight != reader:
                        divergent.append(f"level {level} {square}: {fight} != {reader}")
        assert divergent == []

    @pytest.mark.parametrize("case", corpus(), ids=lambda case: case.name)
    def test_only_a_feature_carrying_a_state_becomes_a_fixture_of_the_fight(
        self, case: MapCase
    ) -> None:
        encounter, document = encounter_on(case)

        state = encounter.state()["map"]
        assert state is not None
        assert set(state["features"]) == {
            record.id
            for level in document.levels.values()
            for record in level.features
            if record.state is not None
        }

    def test_the_corpus_actually_contains_annotations_to_leave_out(self) -> None:
        # The assertion above is vacuous on a map whose every feature is a
        # fixture, so the corpus has to be checked for the thing it is meant to
        # be discriminating about.
        annotations = {
            record.id
            for _name, build, _party, _foes in CORPUS
            for level in parse_document(
                build(), source="corpus", terrain=TERRAIN
            ).levels.values()
            for record in level.features
            if record.state is None
        }
        assert annotations == {"spawn-party", "stair-foot", "stair-head", "brazier"}


class TestTheMapItselfIsNeverWritten:
    def test_a_map_document_is_frozen(self) -> None:
        document = parse_document(chamber_payload(), source="frozen", terrain=TERRAIN)
        with pytest.raises(dataclasses.FrozenInstanceError):
            document.name = "renamed"  # type: ignore[misc]

    def test_operating_a_door_moves_the_overlay_and_not_the_artifact(self) -> None:
        # ``test_analytics`` shares one map across five iterations, but its arena
        # carries no fixtures at all, so a fight that wrote to its map would not
        # surface there. This one has a door and opens it.
        payload = chamber_payload()
        pristine = parse_document(payload, source="pristine", terrain=TERRAIN)
        document = parse_document(payload, source="pristine", terrain=TERRAIN)
        rng = Random(3)
        encounter = Encounter(
            [
                fighter(position=standing_at((3, 3))),
                fighter("Grull", team="foes", position=standing_at((1, 1))),
            ],
            rng,
            map_document=document,
        )
        assert encounter.map_state is not None
        assert encounter.map_state.open_features == set()

        advance_to(encounter, "Thora", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="chamber-door", set_open=True), rng
        )

        assert encounter.map_state.open_features == {"chamber-door"}
        # The change lives in the overlay only: the artifact still equals a map
        # parsed fresh from the same payload, feature record and all.
        assert document == pristine
        assert document.fixtures()["chamber-door"].state == "closed"
