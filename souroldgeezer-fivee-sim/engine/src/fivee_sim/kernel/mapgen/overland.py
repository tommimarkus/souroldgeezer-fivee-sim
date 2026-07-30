"""Overland generation: layered value noise banded into terrain.

Two independent fields — elevation and moisture — are thresholded into
water, plain, forest, hill, and mountain. Original content: the bands and
their defaults are this engine's invention.

Determinism contract
--------------------
One :class:`~random.Random`, handed to :func:`~.noise.fbm_grid` twice: ALL
elevation lattices are drawn first (ascending octave), then all moisture
lattices, and banding draws nothing. Float use follows the noise module's
contract — arithmetic and comparisons only — so output is bit-identical
across platforms, pinned by an exact-hash canary test.
"""

from __future__ import annotations

from dataclasses import dataclass
from random import Random

from ._types import GeneratedFeature, GeneratedMap
from .noise import fbm_grid

__all__ = ["OverlandParams", "generate_overland"]


@dataclass(frozen=True, slots=True)
class OverlandParams:
    """Every knob, with defaults, so provenance can record the full resolution."""

    width: int = 64
    height: int = 64
    scale: float = 24.0
    octaves: int = 4
    persistence: float = 0.5
    lacunarity: float = 2.0
    water_level: float = 0.30
    hill_level: float = 0.62
    mountain_level: float = 0.78
    forest_moisture: float = 0.55


def generate_overland(rng: Random, params: OverlandParams) -> GeneratedMap:
    """Open country from two noise fields, reproducible under a seed.

    Kinds emitted: ``water``, ``plain``, ``forest``, ``hill``, ``mountain``.
    Per cell: elevation below ``water_level`` is water; below ``hill_level``
    it is forest where moisture exceeds ``forest_moisture`` and plain
    otherwise; below ``mountain_level`` it is hill; the rest is mountain.
    The spawn hint sits on the first plain cell in scan order — absent only
    when the thresholds leave no plain at all.
    """
    if not params.water_level <= params.hill_level <= params.mountain_level:
        raise ValueError(
            f"the elevation bands must be ordered water_level <= hill_level <= "
            f"mountain_level, got {params.water_level}, {params.hill_level}, "
            f"{params.mountain_level}"
        )
    elevation = fbm_grid(
        rng, params.width, params.height, scale=params.scale, octaves=params.octaves,
        persistence=params.persistence, lacunarity=params.lacunarity,
    )
    moisture = fbm_grid(
        rng, params.width, params.height, scale=params.scale, octaves=params.octaves,
        persistence=params.persistence, lacunarity=params.lacunarity,
    )

    rows: list[tuple[str, ...]] = []
    for y in range(params.height):
        row: list[str] = []
        for x in range(params.width):
            e = elevation[y][x]
            if e < params.water_level:
                row.append("water")
            elif e < params.hill_level:
                row.append("forest" if moisture[y][x] > params.forest_moisture else "plain")
            elif e < params.mountain_level:
                row.append("hill")
            else:
                row.append("mountain")
        rows.append(tuple(row))

    features: tuple[GeneratedFeature, ...] = ()
    for y in range(params.height):
        for x in range(params.width):
            if rows[y][x] == "plain":
                features = (
                    GeneratedFeature(id="spawn-party", kind="spawn", at=(x, y), team="party"),
                )
                break
        if features:
            break

    return GeneratedMap(
        width=params.width, height=params.height, cells=tuple(rows), features=features
    )
