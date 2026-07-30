"""Cave generation: cellular automata, region culling, corridor joining.

A noisy seed smooths into organic caverns; small pockets are filled; what
remains is joined into a single cave. The output guarantee — exactly one floor
region — is pinned by test.

Determinism contract
--------------------
One :class:`~random.Random`, and a single draw phase: the interior is seeded
wall-or-floor in row-major order (``rng.random()`` per cell), and nothing
after that draws at all. Smoothing is double-buffered so a pass reads only the
previous grid; flood fill labels regions in scan order with a fixed
neighbour order; corridor endpoints are chosen by minimum squared distance
with ties broken by ``(y, x)``, never by set iteration. Floats appear only in
the seeding comparison, so output is bit-identical across platforms.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from random import Random

from ..grid import Square
from ._types import GeneratedFeature, GeneratedMap

__all__ = ["CaveParams", "generate_caves"]


@dataclass(frozen=True, slots=True)
class CaveParams:
    """Every knob, with defaults, so provenance can record the full resolution."""

    width: int = 48
    height: int = 32
    initial_wall_chance: float = 0.45
    smoothing_passes: int = 5
    wall_threshold: int = 5
    min_region: int = 16
    connect_regions: bool = True


def _check(params: CaveParams) -> None:
    if params.width < 3 or params.height < 3:
        raise ValueError(
            f"the map must be at least 3x3 to have an interior, got "
            f"{params.width}x{params.height}"
        )
    if not 0.0 <= params.initial_wall_chance <= 1.0:
        raise ValueError(
            f"initial_wall_chance must be between 0 and 1, got {params.initial_wall_chance}"
        )
    if params.smoothing_passes < 0:
        raise ValueError(f"smoothing_passes must be 0 or more, got {params.smoothing_passes}")
    if not 0 <= params.wall_threshold <= 9:
        raise ValueError(
            f"wall_threshold counts a 9-cell neighbourhood, so it must be between "
            f"0 and 9; got {params.wall_threshold}"
        )
    if params.min_region < 1:
        raise ValueError(f"min_region must be at least 1, got {params.min_region}")


def _regions(wall: list[list[bool]], width: int, height: int) -> list[list[Square]]:
    """Floor regions, 4-connected, labelled in scan order. Fully deterministic."""
    seen = [[False] * width for _ in range(height)]
    found: list[list[Square]] = []
    for start_y in range(height):
        for start_x in range(width):
            if wall[start_y][start_x] or seen[start_y][start_x]:
                continue
            region: list[Square] = []
            queue: deque[Square] = deque([(start_x, start_y)])
            seen[start_y][start_x] = True
            while queue:
                x, y = queue.popleft()
                region.append((x, y))
                for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                    nx, ny = x + dx, y + dy
                    if not (0 <= nx < width and 0 <= ny < height):
                        continue
                    if wall[ny][nx] or seen[ny][nx]:
                        continue
                    seen[ny][nx] = True
                    queue.append((nx, ny))
            found.append(region)
    return found


def _closest_pair(a: list[Square], b: list[Square]) -> tuple[Square, Square]:
    """The closest cells of two regions: minimum squared distance, ties by (y, x).

    The key orders on distance, then on the first region's cell, then the
    second's — so the answer is independent of the regions' internal order.
    """
    best: tuple[int, int, int, int, int] | None = None
    pair = (a[0], b[0])
    for ax, ay in a:
        for bx, by in b:
            d2 = (ax - bx) * (ax - bx) + (ay - by) * (ay - by)
            key = (d2, ay, ax, by, bx)
            if best is None or key < best:
                best = key
                pair = ((ax, ay), (bx, by))
    return pair


def generate_caves(rng: Random, params: CaveParams) -> GeneratedMap:
    """Cellular-automata caverns, reproducible under a seed.

    Kinds emitted: ``wall`` and ``floor``. The border is always wall; a cell
    smooths to wall when at least ``wall_threshold`` of its 9-cell
    neighbourhood (itself included; out of bounds counts as wall) is wall.
    Regions smaller than ``min_region`` are filled. With ``connect_regions``
    the survivors are joined to the largest by one-wide L-corridors
    (horizontal leg first — no draw decides it) between their closest cell
    pairs; without it, only the largest survives. Either way the result is a
    single floor region — or a solid map, if nothing survived the culling.
    The spawn hint sits on the first floor cell in scan order.
    """
    _check(params)
    width, height = params.width, params.height

    # The one draw phase: interior cells in row-major order.
    wall = [[True] * width for _ in range(height)]
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            wall[y][x] = rng.random() < params.initial_wall_chance

    for _ in range(params.smoothing_passes):
        smoothed = [[True] * width for _ in range(height)]
        for y in range(1, height - 1):
            for x in range(1, width - 1):
                neighbours = 0
                for ny in (y - 1, y, y + 1):
                    for nx in (x - 1, x, x + 1):
                        if wall[ny][nx]:
                            neighbours += 1
                smoothed[y][x] = neighbours >= params.wall_threshold
        wall = smoothed

    regions: list[list[Square]] = []
    for region in _regions(wall, width, height):
        if len(region) >= params.min_region:
            regions.append(region)
        else:
            for x, y in region:
                wall[y][x] = True

    if regions:
        largest_index = 0
        for index, region in enumerate(regions):
            if len(region) > len(regions[largest_index]):
                largest_index = index
        largest = regions[largest_index]
        for index, region in enumerate(regions):
            if index == largest_index:
                continue
            if params.connect_regions:
                (ax, ay), (bx, by) = _closest_pair(region, largest)
                for x in range(min(ax, bx), max(ax, bx) + 1):
                    wall[ay][x] = False
                for y in range(min(ay, by), max(ay, by) + 1):
                    wall[y][bx] = False
            else:
                for x, y in region:
                    wall[y][x] = True

    features: tuple[GeneratedFeature, ...] = ()
    for y in range(height):
        for x in range(width):
            if not wall[y][x]:
                features = (
                    GeneratedFeature(id="spawn-party", kind="spawn", at=(x, y), team="party"),
                )
                break
        if features:
            break

    return GeneratedMap(
        width=width, height=height,
        cells=tuple(
            tuple("wall" if wall[y][x] else "floor" for x in range(width))
            for y in range(height)
        ),
        features=features,
    )
