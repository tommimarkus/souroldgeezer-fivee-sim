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
increasing downward, matching :mod:`fivee_sim.kernel.grid`. A square alone does
not locate a creature on a map with storeys: a *level* picks the plane and the
square picks the spot on it. The level is deliberately not folded into the
square — every geometry primitive in :mod:`fivee_sim.kernel.grid` is correct on
one plane, and pushing a third coordinate through them would buy nothing a fight
can use, because a floor blocks what is above and below it anyway.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from ..kernel.grid import Square

__all__ = ["GROUND_LEVEL", "BattleMap", "MapFeature", "MapPlane", "MapState"]

#: The level every fight starts on, and the only one a map without storeys has.
GROUND_LEVEL = 0


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
class MapPlane:
    """One level of the battlefield: everything that varies between storeys.

    ``terrain`` and ``elevation`` are both sparse — only squares that differ from
    ``default_terrain`` and ``default_elevation`` appear. ``features`` is keyed by
    feature name, which is how actions refer to them.

    ``elevation`` is ground height in feet, and it reaches movement only: a step
    onto higher ground is a slope or a climb, and that is the whole of it. Sight,
    cover, and the area templates are measured on the flat, so a ridge screens
    nobody and a creature atop one is no harder to shoot. ``default_elevation``
    doubles as the storey's own floor height, which is why an upper floor needs
    no separate datum: it *is* the height its unnamed squares sit at.

    ``connectors`` maps a square on this plane to the level a creature standing
    there can step to — the stairway, ladder or hatch. The square it arrives on
    is the one it left, one plane over.
    """

    default_terrain: str = "normal"
    terrain: Mapping[Square, str] = field(default_factory=dict)
    default_elevation: int = 0
    elevation: Mapping[Square, int] = field(default_factory=dict)
    features: Mapping[str, MapFeature] = field(default_factory=dict)
    connectors: Mapping[Square, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BattleMap:
    """The static battlefield: dimensions and one plane per level. Frozen.

    Every plane shares ``width`` and ``height`` — storeys of one building, not
    unrelated maps. ``levels`` always holds :data:`GROUND_LEVEL`; a map without
    storeys holds only that, and the accessors below read it, because a caller
    asking a map for its terrain has always meant the ground.

    Feature names are unique across the whole map, so :attr:`features` can merge
    the planes into the single table actions look names up in.
    """

    name: str
    width: int  # squares
    height: int
    levels: Mapping[int, MapPlane] = field(
        default_factory=lambda: MappingProxyType({GROUND_LEVEL: MapPlane()})
    )
    provenance: str = "caller-supplied"

    @property
    def ground(self) -> MapPlane:
        return self.levels[GROUND_LEVEL]

    @property
    def default_terrain(self) -> str:
        return self.ground.default_terrain

    @property
    def terrain(self) -> Mapping[Square, str]:
        return self.ground.terrain

    @property
    def default_elevation(self) -> int:
        return self.ground.default_elevation

    @property
    def elevation(self) -> Mapping[Square, int]:
        return self.ground.elevation

    @property
    def features(self) -> Mapping[str, MapFeature]:
        """Every plane's fixtures under one name table, the ground's first."""
        merged: dict[str, MapFeature] = {}
        for index in sorted(self.levels):
            merged.update(self.levels[index].features)
        return MappingProxyType(merged)

    def level_of(self, feature_name: str) -> int:
        """Which plane holds a named fixture. Raises :class:`KeyError` if none does."""
        for index in sorted(self.levels):
            if feature_name in self.levels[index].features:
                return index
        raise KeyError(feature_name)

    @classmethod
    def flat(
        cls,
        *,
        name: str,
        width: int,
        height: int,
        default_terrain: str = "normal",
        terrain: Mapping[Square, str] | None = None,
        default_elevation: int = 0,
        elevation: Mapping[Square, int] | None = None,
        features: Mapping[str, MapFeature] | None = None,
        provenance: str = "caller-supplied",
    ) -> BattleMap:
        """A one-plane map, which is what a fight without storeys wants.

        The common case by a wide margin, and worth a constructor of its own so
        it does not have to spell out a levels table holding a single entry.
        """
        return cls(
            name=name,
            width=width,
            height=height,
            levels=MappingProxyType(
                {
                    GROUND_LEVEL: MapPlane(
                        default_terrain=default_terrain,
                        terrain=terrain if terrain is not None else {},
                        default_elevation=default_elevation,
                        elevation=elevation if elevation is not None else {},
                        features=features if features is not None else {},
                    )
                }
            ),
            provenance=provenance,
        )


@dataclass(slots=True)
class MapState:
    """What a fight has changed about its map: which features stand open."""

    open_features: set[str]
