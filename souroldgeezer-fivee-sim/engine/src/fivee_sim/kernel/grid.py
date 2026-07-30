"""Square-grid geometry: distance, sight, cover, area templates, pathfinding.

Nothing here knows what a ``Creature`` or a battle map is. Callers hand in the
few facts a question depends on — which squares are opaque, what entering a
square costs — as plain callables, for the same reason every rolling function
takes an explicit ``Random``: geometry read from ambient module state is state a
caller cannot control.

Two coordinate systems appear, and keeping them straight matters. A ``Point`` is
feet on the plane and may be any pair of ints; a ``Square`` is a pair of 5-foot
grid indices, zero-based, origin at the top-left, y increasing downward. The
canonical point of a square is its minimum corner in feet — see
:func:`square_center`.

Everything is exact integer arithmetic. Sight and cover are decided by
orientation tests on corner-lattice segments, never by floating-point sampling,
so two runs — or two machines — cannot disagree about who can see whom.

Sight policy, pinned by test: a corner-to-corner segment is blocked when it
passes through the strict interior of an opaque square, or along the open edge
shared by two edge-adjacent opaque squares. Grazing the outer face of a wall
does not block, and neither does threading the corner point where two opaque
squares touch only diagonally. The endpoints' own squares never block.

Distance, cover grades, and areas of effect follow SRD 5.2 (see NOTICE). The
``5-10-5`` diagonal is the published variant rule; the corner tests and the
terrain kinds are engine policy.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import IntEnum, StrEnum

#: A position in feet on the plane. Any pair of ints; no lattice is implied.
Point = tuple[int, int]
#: A 5-foot grid square: zero-based indices, origin top-left, y downward.
Square = tuple[int, int]

FEET_PER_SQUARE = 5


class DiagonalRule(StrEnum):
    """How diagonals are measured, for movement and areas alike.

    ``5-5-5`` is the SRD rule — every diagonal costs 5 feet, so distance is the
    Chebyshev metric. ``5-10-5`` is the published variant in which every second
    diagonal costs double.
    """

    FIVE_FIVE_FIVE = "5-5-5"
    FIVE_TEN_FIVE = "5-10-5"


class CoverGrade(IntEnum):
    NONE = 0
    HALF = 1
    THREE_QUARTERS = 2
    TOTAL = 3


def cover_ac_bonus(grade: CoverGrade) -> int:
    """The AC (and Dexterity saving throw) bonus a grade of cover grants.

    ``TOTAL`` raises rather than returning a sentinel: a creature with total
    cover cannot be targeted at all, so a caller holding ``TOTAL`` must refuse
    the attack before asking what to add to AC.
    """
    if grade is CoverGrade.TOTAL:
        raise ValueError(
            "total cover grants no AC bonus; a creature with total cover cannot be targeted"
        )
    return {CoverGrade.NONE: 0, CoverGrade.HALF: 2, CoverGrade.THREE_QUARTERS: 5}[grade]


# --- terrain ---------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TerrainEffect:
    """The mechanical consequences of one kind of terrain."""

    #: Multiplies the cost of entering the square; 2 is difficult terrain.
    move_cost_multiplier: int = 1
    passable: bool = True
    #: Blocks sight lines outright — see :func:`has_line_of_sight`.
    opaque: bool = False
    #: The :class:`CoverGrade` value granted when the square sits on a sight line.
    #: Opacity is not folded in here; a caller composing a ``cover_of`` callable
    #: should treat an opaque square as granting ``TOTAL``.
    cover: int = 0


#: Every field a terrain kind may set. Content-pack validation reports this list
#: when a pack names a field that does not exist, so it is derived rather than
#: retyped.
TERRAIN_FLAGS: tuple[str, ...] = tuple(TerrainEffect.__dataclass_fields__)

# Keyed by plain ``str`` so a pack's table and this one are the same type. The
# dungeon kinds come first; the overland kinds are what the terrain generators
# will emit. ``floor`` and ``door-open`` deliberately repeat ``normal``'s
# semantics under their own names: a map that says what a square *is* renders
# and edits better than one that collapsed everything walkable to "normal".
TERRAIN: dict[str, TerrainEffect] = {
    "normal": TerrainEffect(),
    "floor": TerrainEffect(),
    "wall": TerrainEffect(passable=False, opaque=True),
    "difficult": TerrainEffect(move_cost_multiplier=2),
    "half-cover": TerrainEffect(cover=1),
    "three-quarters-cover": TerrainEffect(cover=2),
    "door-open": TerrainEffect(),
    "door-closed": TerrainEffect(passable=False, opaque=True),
    "plain": TerrainEffect(),
    "water": TerrainEffect(move_cost_multiplier=2),
    "forest": TerrainEffect(move_cost_multiplier=2, cover=1),
    "hill": TerrainEffect(move_cost_multiplier=2),
    # A mountain cannot be entered but can be seen over: impassable, not opaque.
    "mountain": TerrainEffect(passable=False),
}

TerrainTable = Mapping[str, TerrainEffect]


class UnknownTerrain(KeyError):
    """A terrain kind was referenced that the active table does not define."""


def terrain_effect_of(kind: str, table: TerrainTable = TERRAIN) -> TerrainEffect:
    """The effects of one terrain kind, or a report of what the table does define.

    Raising rather than defaulting to normal ground is the point: a misspelled
    kind that quietly walks like a meadow produces a map that looks right and
    plays wrongly.
    """
    try:
        return table[kind]
    except KeyError:
        available = ", ".join(sorted(table)) or "none"
        raise UnknownTerrain(
            f"no terrain named {kind!r}; the active content defines: {available}"
        ) from None


# --- points and squares ----------------------------------------------------
def as_point(position: Point | int) -> Point:
    """Widen a scalar position to a point on the x-axis.

    Positions predate the plane: a bare int has always meant feet along one
    axis, and it still does. ``Creature`` widens on construction, but its
    attribute admits both forms, so read sites narrow through here.
    """
    if isinstance(position, int):
        return (position, 0)
    return position


def to_square(point: Point) -> Square:
    """The square a point in feet falls in. Floor division: edges belong down-right."""
    return (point[0] // FEET_PER_SQUARE, point[1] // FEET_PER_SQUARE)


def square_center(square: Square) -> Point:
    """The canonical point of a square: its minimum corner, in feet.

    Not the geometric middle. Distances and templates measure between these
    canonical points, and keeping them on the 5-foot lattice keeps every
    computation in integers. ``to_square(square_center(s)) == s`` always holds.
    """
    return (square[0] * FEET_PER_SQUARE, square[1] * FEET_PER_SQUARE)


def distance_feet(a: Point, b: Point, rule: DiagonalRule = DiagonalRule.FIVE_FIVE_FIVE) -> int:
    """The distance in feet between two points, under a diagonal rule.

    ``5-5-5`` is the Chebyshev metric: the longer axis alone. ``5-10-5`` adds
    half the shorter axis — ``dmax + dmin // 2`` — computed in feet, so
    positions off the 5-foot lattice stay exact.
    """
    dx = abs(a[0] - b[0])
    dy = abs(a[1] - b[1])
    if rule is DiagonalRule.FIVE_TEN_FIVE:
        return max(dx, dy) + min(dx, dy) // 2
    return max(dx, dy)


# --- sight and cover -------------------------------------------------------
def _corners(square: Square) -> tuple[tuple[int, int], ...]:
    """The square's four corners, on the corner lattice (units of squares)."""
    x, y = square
    return ((x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1))


def _enters_interior(x0: int, y0: int, dx: int, dy: int, square: Square) -> bool:
    """Whether the segment from ``(x0, y0)`` along ``(dx, dy)`` crosses the
    square's strict interior. Exact: bounding-interval and orientation tests only.
    """
    sx, sy = square
    x1, y1 = x0 + dx, y0 + dy
    if max(x0, x1) <= sx or min(x0, x1) >= sx + 1:
        return False
    if max(y0, y1) <= sy or min(y0, y1) >= sy + 1:
        return False
    # The line crosses the open box exactly when the box has corners strictly on
    # both sides of it; with the interval checks above, so does the segment.
    positive = negative = False
    for cx, cy in ((sx, sy), (sx + 1, sy), (sx, sy + 1), (sx + 1, sy + 1)):
        cross = dx * (cy - y0) - dy * (cx - x0)
        positive = positive or cross > 0
        negative = negative or cross < 0
    return positive and negative


def _line_blockers(
    start: tuple[int, int],
    end: tuple[int, int],
    blocks: Callable[[Square], bool],
    exempt: frozenset[Square],
) -> frozenset[Square]:
    """Every blocking square the corner-to-corner segment cannot avoid.

    Empty means the line is clear. An axis-aligned segment runs along a gridline
    and grazes the squares on either side; it is blocked only where *both* sides
    block — the seam inside a solid wall — never by a face it merely skims. An
    oblique segment is blocked by any square whose strict interior it enters,
    which subsumes every transversal crossing, so no separate edge case exists.
    """
    x0, y0 = start
    x1, y1 = end
    if (x0, y0) == (x1, y1):
        return frozenset()
    found: set[Square] = set()

    def blocking(square: Square) -> bool:
        return square not in exempt and blocks(square)

    if y0 == y1:
        lo, hi = (x0, x1) if x0 < x1 else (x1, x0)
        for sx in range(lo, hi):
            above = (sx, y0 - 1)
            below = (sx, y0)
            if blocking(above) and blocking(below):
                found.update((above, below))
        return frozenset(found)
    if x0 == x1:
        lo, hi = (y0, y1) if y0 < y1 else (y1, y0)
        for sy in range(lo, hi):
            left = (x0 - 1, sy)
            right = (x0, sy)
            if blocking(left) and blocking(right):
                found.update((left, right))
        return frozenset(found)

    if x1 < x0:  # canonicalise left to right; a segment is symmetric
        x0, y0, x1, y1 = x1, y1, x0, y0
    dx = x1 - x0
    dy = y1 - y0
    for sx in range(x0, x1):
        # y at the column's edges, as exact rationals over the denominator dx.
        ya = y0 * dx + dy * (sx - x0)
        yb = y0 * dx + dy * (sx + 1 - x0)
        lo, hi = (ya, yb) if ya < yb else (yb, ya)
        # Padded by a row on each side; the interior test is the exact judge.
        for sy in range(lo // dx - 1, hi // dx + 2):
            square = (sx, sy)
            if blocking(square) and _enters_interior(x0, y0, dx, dy, square):
                found.add(square)
    return frozenset(found)


def has_line_of_sight(a: Square, b: Square, *, opaque: Callable[[Square], bool]) -> bool:
    """Whether any corner of ``a`` sees any corner of ``b``.

    Any-corner-to-any-corner: visible exactly when some segment between the two
    squares' corners avoids every opaque square, under the sight policy in the
    module docstring. Symmetric by construction, and the endpoints' own squares
    never block — same-square and adjacent squares trivially see each other.
    """
    if a == b:
        return True
    exempt = frozenset((a, b))
    return any(
        not _line_blockers(start, end, opaque, exempt)
        for start in _corners(a)
        for end in _corners(b)
    )


_COUNT_GRADE = {
    0: CoverGrade.NONE,
    1: CoverGrade.HALF,
    2: CoverGrade.HALF,
    3: CoverGrade.THREE_QUARTERS,
    4: CoverGrade.TOTAL,
}


def cover_between(
    attacker: Square,
    target: Square,
    *,
    cover_of: Callable[[Square], int],
    occupied: frozenset[Square] = frozenset(),
) -> CoverGrade:
    """The cover ``target`` has against ``attacker``, by the corner-count rule.

    From each attacker corner, count the blocked lines to the target's four
    corners; the attacker uses its best corner. 0 blocked is no cover, 1-2 half,
    3 three-quarters, 4 total. A line is blocked by any square granting cover
    (``cover_of``, the :class:`CoverGrade` value it grants — a caller composing
    it from terrain should treat opaque squares as granting ``TOTAL``) and by
    any occupied square.

    A blocker can only grant what it carries: creatures grant half cover at
    most, and a half-cover screen stays half cover however many lines it
    blocks — the corner count is capped by the strongest contributing grade.
    With walls granting ``TOTAL``, the result is ``TOTAL`` exactly when
    :func:`has_line_of_sight` is false, which a test pins. The attacker's and
    target's own squares never block.
    """
    if attacker == target:
        return CoverGrade.NONE
    exempt = frozenset((attacker, target))

    def blocks(square: Square) -> bool:
        return cover_of(square) > 0 or square in occupied

    best = CoverGrade.TOTAL
    for start in _corners(attacker):
        blocked = 0
        strongest = CoverGrade.NONE
        for end in _corners(target):
            blockers = _line_blockers(start, end, blocks, exempt)
            if not blockers:
                continue
            blocked += 1
            for square in blockers:
                granted = min(cover_of(square), int(CoverGrade.TOTAL))
                if square in occupied:
                    granted = max(granted, int(CoverGrade.HALF))
                strongest = max(strongest, CoverGrade(granted))
        grade = min(_COUNT_GRADE[blocked], strongest) if blocked else CoverGrade.NONE
        best = min(best, grade)
        if best is CoverGrade.NONE:
            break
    return best


# --- area templates --------------------------------------------------------
def sphere_squares(
    center: Square,
    radius_feet: int,
    *,
    rule: DiagonalRule = DiagonalRule.FIVE_FIVE_FIVE,
) -> frozenset[Square]:
    """The squares within ``radius_feet`` of ``center``, centre included.

    Membership is :func:`distance_feet` between canonical points, so the
    diagonal knob governs areas exactly as it governs movement: under ``5-5-5``
    a 20-foot radius is the 9-by-9 Chebyshev disc.
    """
    if radius_feet < 0:
        raise ValueError(f"radius must be 0 or more feet, got {radius_feet}")
    reach = radius_feet // FEET_PER_SQUARE
    origin = square_center(center)
    return frozenset(
        (center[0] + dx, center[1] + dy)
        for dx in range(-reach, reach + 1)
        for dy in range(-reach, reach + 1)
        if distance_feet(origin, square_center((center[0] + dx, center[1] + dy)), rule)
        <= radius_feet
    )


_CARDINALS = frozenset({(1, 0), (-1, 0), (0, 1), (0, -1)})
_DIAGONALS = frozenset({(1, 1), (1, -1), (-1, 1), (-1, -1)})


def cone_squares(
    origin: Square, direction: tuple[int, int], length_feet: int
) -> frozenset[Square]:
    """The wedge of a cone aimed along one of the eight unit offsets.

    Deterministic rasters, origin excluded. A cardinal cone at range ``d``
    squares spans the squares within ``d // 2`` of its centreline; a diagonal
    cone is the same wedge reflected onto the diagonal. All four cardinal
    aims raster identically by rotation, as do all four diagonals — pinned by
    test rather than left to the reflection arithmetic.
    """
    length = length_feet // FEET_PER_SQUARE
    ox, oy = origin
    out: set[Square] = set()
    if direction in _CARDINALS:
        fx, fy = direction
        for ahead in range(1, length + 1):
            for side in range(-(ahead // 2), ahead // 2 + 1):
                if fx:
                    out.add((ox + fx * ahead, oy + side))
                else:
                    out.add((ox + side, oy + fy * ahead))
    elif direction in _DIAGONALS:
        fx, fy = direction
        for da in range(length + 1):
            for db in range(length + 1):
                longer = max(da, db)
                if not 1 <= longer <= length:
                    continue
                if 2 * min(da, db) < longer - 1:
                    continue
                out.add((ox + fx * da, oy + fy * db))
    else:
        raise ValueError(
            f"direction must be one of the 8 unit offsets, got {direction!r}"
        )
    return frozenset(out)


def _supercover(start: Square, end: Square) -> list[Square]:
    """Every square the centre-to-centre line touches, in walk order.

    Supercover Bresenham: when the line passes exactly through a corner, both
    grazed side squares are included — the x-side square first, a fixed order so
    truncation is deterministic.
    """
    x0, y0 = start
    x1, y1 = end
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    step_x = 1 if x1 >= x0 else -1
    step_y = 1 if y1 >= y0 else -1
    x, y = x0, y0
    out: list[Square] = [(x, y)]
    ix = iy = 0
    while ix < dx or iy < dy:
        decision = (1 + 2 * ix) * dy - (1 + 2 * iy) * dx
        if decision == 0:
            out.append((x + step_x, y))
            out.append((x, y + step_y))
            x += step_x
            y += step_y
            ix += 1
            iy += 1
            out.append((x, y))
        elif decision < 0:
            x += step_x
            ix += 1
            out.append((x, y))
        else:
            y += step_y
            iy += 1
            out.append((x, y))
    return out


def line_squares(origin: Square, toward: Square, length_feet: int) -> frozenset[Square]:
    """The squares of a line from ``origin`` toward ``toward``, origin excluded.

    A supercover Bresenham walk truncated at ``length_feet // 5`` squares, so a
    grazed corner pair counts against the length in walk order. Aiming at a
    square nearer than the length simply ends the line there — extending the
    ray is the caller's choice, made by aiming further.
    """
    length = length_feet // FEET_PER_SQUARE
    return frozenset(_supercover(origin, toward)[1 : 1 + length])


def cube_squares(min_corner: Square, size_feet: int) -> frozenset[Square]:
    """An n-by-n block of squares, ``n = size_feet // 5``, from its minimum corner."""
    size = size_feet // FEET_PER_SQUARE
    x, y = min_corner
    return frozenset((x + i, y + j) for i in range(size) for j in range(size))


# --- pathfinding -----------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Path:
    """A walkable route: every square from start to destination inclusive."""

    squares: tuple[Square, ...]
    cost_feet: int


_NEIGHBOUR_OFFSETS: tuple[tuple[int, int], ...] = (
    (-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1),
)


def find_path(
    start: Square,
    goal: Square,
    *,
    entry_cost: Callable[[Square], int | None],
    rule: DiagonalRule = DiagonalRule.FIVE_FIVE_FIVE,
    bounds: tuple[int, int],
    blocked: frozenset[Square] = frozenset(),
    stop_adjacent: bool = False,
    max_cost: int | None = None,
) -> Path | None:
    """The cheapest route from ``start`` to ``goal``, or ``None`` if there is none.

    A* over the 8-connected grid inside ``bounds`` (width, height). ``entry_cost``
    is the feet it costs to enter a square — terrain multipliers already applied —
    or ``None`` for impassable ground; ``blocked`` removes squares outright, which
    is what occupancy is. Under ``5-10-5`` the search state carries diagonal
    parity, and the second diagonal of each pair costs double its entry cost.

    ``stop_adjacent`` accepts any square Chebyshev-adjacent to the goal — the way
    to walk *to* a creature rather than onto it. ``max_cost`` abandons routes over
    budget. The result is deterministic: the heap orders equal-cost candidates
    lexicographically by square, never by insertion order, and a test pins the
    tie-break. The heuristic is :func:`distance_feet` to the nearest accepting
    square; with entry costs in multiples of 5 — every terrain multiplier is —
    the returned path is cost-optimal under either rule.
    """
    width, height = bounds

    def in_bounds(square: Square) -> bool:
        return 0 <= square[0] < width and 0 <= square[1] < height

    if not (in_bounds(start) and in_bounds(goal)):
        return None

    if stop_adjacent:
        accepting = tuple(
            square
            for square in sorted(
                {goal} | {(goal[0] + ox, goal[1] + oy) for ox, oy in _NEIGHBOUR_OFFSETS}
            )
            if in_bounds(square)
        )
    else:
        accepting = (goal,)
    accept = frozenset(accepting)
    targets = tuple(square_center(square) for square in accepting)

    def heuristic(square: Square) -> int:
        centre = square_center(square)
        return min(distance_feet(centre, target, rule) for target in targets)

    if start in accept:
        return Path(squares=(start,), cost_feet=0)

    start_node = (start, 0)
    best_g: dict[tuple[Square, int], int] = {start_node: 0}
    parents: dict[tuple[Square, int], tuple[Square, int]] = {}
    open_heap: list[tuple[int, Square, int]] = [(heuristic(start), start, 0)]
    settled: set[tuple[Square, int]] = set()

    while open_heap:
        _f, square, parity = heapq.heappop(open_heap)
        node = (square, parity)
        if node in settled:
            continue
        settled.add(node)
        cost_so_far = best_g[node]
        if square in accept:
            squares = [square]
            walk = node
            while walk in parents:
                walk = parents[walk]
                squares.append(walk[0])
            squares.reverse()
            return Path(squares=tuple(squares), cost_feet=cost_so_far)
        for ox, oy in _NEIGHBOUR_OFFSETS:
            step_to = (square[0] + ox, square[1] + oy)
            if not in_bounds(step_to) or step_to in blocked:
                continue
            entering = entry_cost(step_to)
            if entering is None:
                continue
            if rule is DiagonalRule.FIVE_TEN_FIVE and ox and oy:
                step = entering * 2 if parity else entering
                next_parity = parity ^ 1
            else:
                step = entering
                next_parity = parity
            found_g = cost_so_far + step
            if max_cost is not None and found_g > max_cost:
                continue
            next_node = (step_to, next_parity)
            known = best_g.get(next_node)
            if known is not None and known <= found_g:
                continue
            best_g[next_node] = found_g
            parents[next_node] = node
            heapq.heappush(open_heap, (found_g + heuristic(step_to), step_to, next_parity))
    return None
