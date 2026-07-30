"""The shape every generator returns: a grid of terrain kinds plus features.

Defined in a module of their own so the generator modules can share them
without importing the package ``__init__``; the package re-exports both, and
``fivee_sim.kernel.mapgen`` is the public spelling.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..grid import Square

__all__ = ["GeneratedFeature", "GeneratedMap"]


@dataclass(frozen=True, slots=True)
class GeneratedFeature:
    """One feature a generator placed: a door, stairs, or a spawn hint."""

    id: str
    kind: str
    at: Square
    orientation: str | None = None
    state: str | None = None
    team: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedMap:
    """A generator's raw output, in terrain-kind strings.

    ``cells`` is row-major — ``cells[y][x]`` — matching the tile rows of a map
    document (top row first, x rightward). Every kind emitted by a bundled
    generator exists in :data:`fivee_sim.kernel.grid.TERRAIN`, pinned by test.
    """

    width: int
    height: int
    cells: tuple[tuple[str, ...], ...]
    features: tuple[GeneratedFeature, ...]
