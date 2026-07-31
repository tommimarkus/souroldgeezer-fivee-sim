"""Overland generation: layered value noise banded into terrain and height.

Two independent fields — elevation and moisture — are thresholded into
water, plain, forest, hill, and mountain. Original content: the bands and
their defaults are this engine's invention.

Ground height is read off the *same* elevation field the bands come from,
rather than drawn separately: one value per cell, so relief varies inside a
band and a mountain is never lower than a hill. Both mappings are monotonic
in the field, which is what makes that ordering hold by construction.

Determinism contract
--------------------
One :class:`~random.Random`, handed to :func:`~.noise.fbm_grid` twice: ALL
elevation lattices are drawn first (ascending octave), then all moisture
lattices, and neither banding nor the height mapping draws anything. Float
use follows the noise module's contract — arithmetic and comparisons only —
so output is bit-identical across platforms, pinned by an exact-hash canary
test.
"""

from __future__ import annotations

from dataclasses import dataclass
from random import Random

from ._types import GeneratedFeature, GeneratedMap
from .noise import fbm_grid

__all__ = ["HEIGHT_STEP_FEET", "OverlandParams", "generate_overland"]

#: Heights are quantised to this many feet. It is
#: :data:`~fivee_sim.kernel.grid.CLIMB_FEET`, and deliberately: under a 5-foot
#: step every difference between neighbours would fall in the gentle-slope band
#: and the relief would cost a mover nothing at all.
HEIGHT_STEP_FEET = 5


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
    relief_feet: int = 40
    water_depth_feet: int = 20


def generate_overland(rng: Random, params: OverlandParams) -> GeneratedMap:
    """Open country from two noise fields, reproducible under a seed.

    Kinds emitted: ``water``, ``plain``, ``forest``, ``hill``, ``mountain``.
    Per cell: elevation below ``water_level`` is water; below ``hill_level``
    it is forest where moisture exceeds ``forest_moisture`` and plain
    otherwise; below ``mountain_level`` it is hill; the rest is mountain.
    The spawn hint sits on the first plain cell in scan order — absent only
    when the thresholds leave no plain at all.

    Every cell also gets a height in feet, quantised to
    :data:`HEIGHT_STEP_FEET`. The waterline is the datum: land rises from zero
    to ``relief_feet`` at the top of the field, water falls to
    ``-water_depth_feet`` at the bottom. Setting both to zero is how to ask for
    the flat map this generator used to produce.
    """
    if not params.water_level <= params.hill_level <= params.mountain_level:
        raise ValueError(
            f"the elevation bands must be ordered water_level <= hill_level <= "
            f"mountain_level, got {params.water_level}, {params.hill_level}, "
            f"{params.mountain_level}"
        )
    if params.relief_feet < 0 or params.water_depth_feet < 0:
        raise ValueError(
            f"relief cannot be negative, got relief_feet={params.relief_feet} and "
            f"water_depth_feet={params.water_depth_feet}"
        )
    elevation = fbm_grid(
        rng, params.width, params.height, scale=params.scale, octaves=params.octaves,
        persistence=params.persistence, lacunarity=params.lacunarity,
    )
    moisture = fbm_grid(
        rng, params.width, params.height, scale=params.scale, octaves=params.octaves,
        persistence=params.persistence, lacunarity=params.lacunarity,
    )

    rise_steps = params.relief_feet // HEIGHT_STEP_FEET
    depth_steps = params.water_depth_feet // HEIGHT_STEP_FEET
    land_span = 1.0 - params.water_level

    def feet_at(e: float) -> int:
        """The field value as feet: the waterline is the datum, and it is zero.

        Monotonic non-decreasing in ``e``, which is the property the band
        ordering rests on. Both ends fan out from ``water_level`` rather than
        from the field's own range, so two maps generated with the same knobs
        are directly comparable in height.
        """
        if e < params.water_level:  # water_level > e >= 0, so it cannot be zero
            below = (params.water_level - e) / params.water_level
            return -int(below * depth_steps + 0.5) * HEIGHT_STEP_FEET
        if land_span <= 0.0:  # every cell but the very top is water
            return 0
        above = (e - params.water_level) / land_span
        return int(above * rise_steps + 0.5) * HEIGHT_STEP_FEET

    rows: list[tuple[str, ...]] = []
    heights: list[tuple[int, ...]] = []
    for y in range(params.height):
        row: list[str] = []
        height_row: list[int] = []
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
            height_row.append(feet_at(e))
        rows.append(tuple(row))
        heights.append(tuple(height_row))

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
        width=params.width, height=params.height, cells=tuple(rows), features=features,
        elevation=tuple(heights),
    )
