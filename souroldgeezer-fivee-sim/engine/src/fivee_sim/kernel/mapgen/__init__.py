"""Map generators: dungeons, caves, and open country. Pure, seeded, original.

Three generators, one discipline. Each takes an explicit
:class:`~random.Random` and a frozen params dataclass, performs no I/O, and
draws in a documented order with floats restricted to arithmetic and
comparisons — so the same seed and params produce bit-identical output on any
platform, which the exact-hash canary tests pin. Every terrain kind emitted
exists in :data:`fivee_sim.kernel.grid.TERRAIN`.

The output is a :class:`GeneratedMap` of terrain-kind strings;
:func:`fivee_sim.maps.document_from` encodes one into a map document. All
generated layouts are this engine's original content — nothing here derives
from published game material.
"""

from ._types import GeneratedFeature, GeneratedMap
from .bsp import DungeonParams, generate_dungeon
from .caves import CaveParams, generate_caves
from .noise import fbm_grid
from .overland import OverlandParams, generate_overland

__all__ = [
    "CaveParams",
    "DungeonParams",
    "GeneratedFeature",
    "GeneratedMap",
    "OverlandParams",
    "fbm_grid",
    "generate_caves",
    "generate_dungeon",
    "generate_overland",
]
