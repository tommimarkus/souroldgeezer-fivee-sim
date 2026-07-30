"""Seeded value noise: lattice draws, smoothstep bilinear sampling, fBm.

Determinism contract
--------------------
One :class:`~random.Random`. Per octave, in ascending octave order, a lattice
of ``rng.random()`` values is drawn row-major, sized to cover the map at that
octave's period plus one node in each direction; nothing else draws. Sampling
uses smoothstep-faded bilinear interpolation — ``t*t*(3-2*t)`` — and every
float operation is add, subtract, multiply, divide, or a comparison. No
``math.*`` transcendentals, no ``**``: octave amplitude and frequency are
built by successive multiplication. IEEE 754 doubles make each of those
operations exactly rounded, so the field is bit-identical across platforms.
"""

from __future__ import annotations

from random import Random

__all__ = ["fbm_grid"]


def fbm_grid(
    rng: Random,
    width: int,
    height: int,
    *,
    scale: float,
    octaves: int,
    persistence: float,
    lacunarity: float,
) -> list[list[float]]:
    """A fractional-Brownian-motion value-noise field in ``[0, 1]``.

    ``scale`` is the base octave's period in cells; each further octave
    multiplies frequency by ``lacunarity`` and amplitude by ``persistence``,
    and the weighted sum is normalised by the total amplitude.
    """
    if width < 1 or height < 1:
        raise ValueError(f"the field must be at least 1x1, got {width}x{height}")
    if scale <= 0.0:
        raise ValueError(f"scale must be positive, got {scale}")
    if octaves < 1:
        raise ValueError(f"octaves must be at least 1, got {octaves}")
    if persistence <= 0.0:
        raise ValueError(f"persistence must be positive, got {persistence}")
    if lacunarity < 1.0:
        raise ValueError(f"lacunarity must be at least 1, got {lacunarity}")

    values = [[0.0] * width for _ in range(height)]
    total_amplitude = 0.0
    amplitude = 1.0
    frequency = 1.0
    for _octave in range(octaves):
        lattice_width = int((width - 1) * frequency / scale) + 2
        lattice_height = int((height - 1) * frequency / scale) + 2
        lattice = [
            [rng.random() for _i in range(lattice_width)] for _j in range(lattice_height)
        ]
        for y in range(height):
            v = y * frequency / scale
            j = int(v)
            tv = v - j
            fv = tv * tv * (3.0 - 2.0 * tv)
            above = lattice[j]
            below = lattice[j + 1]
            row = values[y]
            for x in range(width):
                u = x * frequency / scale
                i = int(u)
                tu = u - i
                fu = tu * tu * (3.0 - 2.0 * tu)
                top = above[i] + (above[i + 1] - above[i]) * fu
                bottom = below[i] + (below[i + 1] - below[i]) * fu
                row[x] += (top + (bottom - top) * fv) * amplitude
        total_amplitude += amplitude
        amplitude *= persistence
        frequency *= lacunarity

    for row in values:
        for x in range(width):
            row[x] /= total_amplitude
    return values
