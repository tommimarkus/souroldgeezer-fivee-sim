"""The battle map: a static artifact, and the sliver of it a fight can change.

Two models on purpose, not one. A :class:`BattleMap` is the authored or generated
artifact — dimensions, terrain, features — and it is frozen: nothing that happens
in a fight writes to it, so the same map can back any number of encounters at
once and a finished fight leaves it exactly as it found it. :class:`MapState` is
the encounter-time overlay: the one mutable fact a fight owns about its map is
which features currently stand open. An :class:`~.encounter.Encounter`
*references* a map and layers its own state over it; it never absorbs the map
into itself.

Terrain is named by plain strings, resolved against whatever terrain table the
encounter captured — the same discipline as conditions, and for the same reason:
a pack may define kinds this module has never heard of. Ground height, by
contrast, is just feet, and needs no table to interpret.

Coordinates are 5-foot grid squares, zero-based, origin at the top-left with y
increasing downward, matching :mod:`fivee_sim.kernel.grid`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ..kernel.grid import Square

__all__ = ["BattleMap", "MapFeature", "MapState"]


@dataclass(frozen=True, slots=True)
class MapFeature:
    """One interactable fixture on the map — a door, in the common case.

    The feature owns a pair of terrain kinds, one for each state, and the
    encounter's overlay decides which is in force. The map itself stores only the
    default via ``initially_open``.
    """

    name: str
    square: Square
    kind: str = "door"
    closed_terrain: str = "door-closed"
    open_terrain: str = "door-open"
    initially_open: bool = False


@dataclass(frozen=True, slots=True)
class BattleMap:
    """The static battlefield: dimensions, terrain, height, and fixtures. Frozen.

    ``terrain`` and ``elevation`` are both sparse — only squares that differ from
    ``default_terrain`` and ``default_elevation`` appear. ``features`` is keyed by
    feature name, which is how actions refer to them.

    ``elevation`` is ground height in feet, and it reaches movement only: a step
    onto higher ground is a slope or a climb, and that is the whole of it. Sight,
    cover, and the area templates are measured on the flat, so a ridge screens
    nobody and a creature atop one is no harder to shoot.
    """

    name: str
    width: int  # squares
    height: int
    default_terrain: str = "normal"
    terrain: Mapping[Square, str] = field(default_factory=dict)
    default_elevation: int = 0
    elevation: Mapping[Square, int] = field(default_factory=dict)
    features: Mapping[str, MapFeature] = field(default_factory=dict)
    provenance: str = "caller-supplied"


@dataclass(slots=True)
class MapState:
    """What a fight has changed about its map: which features stand open."""

    open_features: set[str]
