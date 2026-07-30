"""Dungeon generation: binary space partition, rooms, corridors, doors.

The interior (inside a one-cell wall border) is split recursively into leaves,
each leaf gets one room, and corridors join the rooms along the split tree —
a spanning tree, so every room is reachable by construction — plus a few extra
loops. Doors land where a corridor first pierces a room's perimeter, and only
where the doorway survives every later carve: a candidate whose perpendicular
neighbours are no longer solid wall once all corridors are cut is dropped
(without consuming randomness), so every door sits in a true doorway.

Determinism contract
--------------------
One :class:`~random.Random`, drawn in this order and no other:

1. **Splitting**, preorder. Per node that splits: an axis draw
   (``rng.random()``, only when neither splittability nor the aspect bias
   forces the axis), then the cut position (``rng.randint``).
2. **Rooms**, one per leaf in traversal order: width, height, x, y
   (``rng.randint`` each).
3. **Corridors**, postorder over the internal nodes, then each extra
   connection. Per corridor: one leg-order draw (``rng.random()``), then —
   every corridor starts at a room centre and always exits it — one door
   draw (``rng.random()``). Each extra connection first draws its two room
   indices (``rng.randrange`` twice); maps with fewer than two rooms skip
   the extras without drawing.

No draw is fed by unordered iteration, and floats appear only in the aspect
comparison and the chance thresholds — multiply and compare, nothing else —
so output is bit-identical across platforms.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from random import Random

from ..grid import Square
from ._types import GeneratedFeature, GeneratedMap

__all__ = ["DungeonParams", "generate_dungeon"]


@dataclass(frozen=True, slots=True)
class DungeonParams:
    """Every knob, with defaults, so provenance can record the full resolution."""

    width: int = 48
    height: int = 32
    min_room: int = 4
    max_room: int = 12
    min_leaf: int = 8
    split_bias: float = 1.25
    door_chance: float = 0.75
    extra_connections: int = 2


@dataclass(frozen=True, slots=True)
class _Rect:
    x: int
    y: int
    w: int
    h: int

    def centre(self) -> Square:
        return (self.x + self.w // 2, self.y + self.h // 2)

    def contains(self, square: Square) -> bool:
        return self.x <= square[0] < self.x + self.w and self.y <= square[1] < self.y + self.h


@dataclass(slots=True)
class _Node:
    region: _Rect
    first: _Node | None = None
    second: _Node | None = None
    room: _Rect | None = None


def _check(params: DungeonParams) -> None:
    if params.min_room < 2:
        raise ValueError(f"min_room must be at least 2, got {params.min_room}")
    if params.max_room < params.min_room:
        raise ValueError(
            f"max_room ({params.max_room}) must be at least min_room ({params.min_room})"
        )
    if params.min_leaf < params.min_room + 2:
        raise ValueError(
            f"min_leaf ({params.min_leaf}) must be at least min_room + 2 "
            f"({params.min_room + 2}), or a leaf could not hold its room"
        )
    smallest = params.min_room + 4
    if params.width < smallest or params.height < smallest:
        raise ValueError(
            f"the map must be at least {smallest}x{smallest} for min_room="
            f"{params.min_room}, got {params.width}x{params.height}"
        )
    if not 0.0 <= params.door_chance <= 1.0:
        raise ValueError(f"door_chance must be between 0 and 1, got {params.door_chance}")
    if params.extra_connections < 0:
        raise ValueError(f"extra_connections must be 0 or more, got {params.extra_connections}")


def _split(rng: Random, region: _Rect, min_leaf: int, bias: float, leaves: list[_Node]) -> _Node:
    """Preorder recursive split; leaves are appended in traversal order."""
    node = _Node(region=region)
    can_w = region.w > 2 * min_leaf
    can_h = region.h > 2 * min_leaf
    if not can_w and not can_h:
        leaves.append(node)
        return node
    if can_w and not can_h:
        vertical = True
    elif can_h and not can_w:
        vertical = False
    elif region.w > region.h * bias:
        vertical = True
    elif region.h > region.w * bias:
        vertical = False
    else:
        vertical = rng.random() < 0.5
    if vertical:
        cut = rng.randint(min_leaf, region.w - min_leaf)
        first = _Rect(region.x, region.y, cut, region.h)
        second = _Rect(region.x + cut, region.y, region.w - cut, region.h)
    else:
        cut = rng.randint(min_leaf, region.h - min_leaf)
        first = _Rect(region.x, region.y, region.w, cut)
        second = _Rect(region.x, region.y + cut, region.w, region.h - cut)
    node.first = _split(rng, first, min_leaf, bias, leaves)
    node.second = _split(rng, second, min_leaf, bias, leaves)
    return node


def _first_room(node: _Node) -> _Rect:
    """The first room, in leaf traversal order, within a subtree."""
    while node.first is not None:
        node = node.first
    room = node.room
    if room is None:  # pragma: no cover - every leaf is given a room first
        raise AssertionError("corridors run after rooms; every leaf has one")
    return room


def _span(a: int, b: int) -> range:
    """Walk inclusively from ``a`` to ``b`` in either direction."""
    return range(a, b + 1) if b >= a else range(a, b - 1, -1)


def _l_path(a: Square, b: Square, horizontal_first: bool) -> list[Square]:
    """The cells of an L-corridor between two squares, endpoints included."""
    (ax, ay), (bx, by) = a, b
    path: list[Square] = []
    if horizontal_first:
        legs = [((x, ay) for x in _span(ax, bx)), ((bx, y) for y in _span(ay, by))]
    else:
        legs = [((ax, y) for y in _span(ay, by)), ((x, by) for x in _span(ax, bx))]
    for leg in legs:
        for cell in leg:
            if not path or path[-1] != cell:
                path.append(cell)
    return path


def generate_dungeon(rng: Random, params: DungeonParams) -> GeneratedMap:
    """Rooms and corridors inside a solid border, reproducible under a seed.

    Kinds emitted: ``wall`` and ``floor``. Door cells are carved floor — the
    door *feature* supplies the blocking, and its recorded state is always
    ``closed``. A corridor carving east or west pierces a ``vertical``
    doorway; north or south, a ``horizontal`` one. Stairs up and the party
    spawn sit in the first room; stairs down in the room whose centre is
    farthest by breadth-first search over the carved floor (ties keep the
    earlier room), placed without consuming any randomness.
    """
    _check(params)
    width, height = params.width, params.height
    cells = [["wall"] * width for _ in range(height)]

    leaves: list[_Node] = []
    interior = _Rect(1, 1, width - 2, height - 2)
    root = _split(rng, interior, params.min_leaf, params.split_bias, leaves)

    # Rooms: one per leaf, in traversal order. Draws: width, height, x, y.
    rooms: list[_Rect] = []
    room_cells: set[Square] = set()
    for leaf in leaves:
        region = leaf.region
        room_w = rng.randint(params.min_room, min(params.max_room, region.w - 2))
        room_h = rng.randint(params.min_room, min(params.max_room, region.h - 2))
        room_x = rng.randint(region.x + 1, region.x + region.w - room_w - 1)
        room_y = rng.randint(region.y + 1, region.y + region.h - room_h - 1)
        room = _Rect(room_x, room_y, room_w, room_h)
        leaf.room = room
        rooms.append(room)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                cells[y][x] = "floor"
                room_cells.add((x, y))

    candidates: list[tuple[Square, str]] = []
    door_cells: set[Square] = set()

    def carve_corridor(a: Square, b: Square) -> None:
        path = _l_path(a, b, horizontal_first=rng.random() < 0.5)
        for x, y in path:
            cells[y][x] = "floor"
        # The first perimeter piercing: the walk starts inside a room and
        # always leaves it, so the transition exists. The doorway is the cell
        # on the wall line — the one *outside* the room.
        for previous, current in zip(path, path[1:], strict=False):
            if (previous in room_cells) == (current in room_cells):
                continue
            pierce = current if previous in room_cells else previous
            step_x = current[0] - previous[0]
            orientation = "vertical" if step_x else "horizontal"
            if rng.random() < params.door_chance and pierce not in door_cells:
                door_cells.add(pierce)
                candidates.append((pierce, orientation))
            break

    def connect(node: _Node) -> None:
        """Postorder: children first, then join their first rooms."""
        if node.first is None or node.second is None:
            return
        connect(node.first)
        connect(node.second)
        carve_corridor(_first_room(node.first).centre(), _first_room(node.second).centre())

    connect(root)

    if len(rooms) >= 2:
        for _ in range(params.extra_connections):
            a = rng.randrange(len(rooms))
            b = rng.randrange(len(rooms) - 1)
            if b >= a:
                b += 1
            carve_corridor(rooms[a].centre(), rooms[b].centre())

    # Candidates become doors only where the doorway is still a doorway once
    # all carving is done: a later corridor may widen the opening, and a door
    # needs solid wall on both perpendicular sides. Filtering after the fact
    # keeps the draw order fixed while making the doorway property true by
    # construction. Consumes no randomness.
    doors: list[GeneratedFeature] = []
    for (x, y), orientation in candidates:
        if orientation == "vertical":
            solid = cells[y - 1][x] == "wall" and cells[y + 1][x] == "wall"
        else:
            solid = cells[y][x - 1] == "wall" and cells[y][x + 1] == "wall"
        if solid:
            doors.append(
                GeneratedFeature(
                    id=f"door-{len(doors) + 1}", kind="door", at=(x, y),
                    orientation=orientation, state="closed",
                )
            )

    # Stairs and spawn consume no randomness.
    first = rooms[0]
    up_at = first.centre()
    beside = (up_at[0] + 1, up_at[1])
    spawn_at = beside if first.contains(beside) else (up_at[0] - 1, up_at[1])

    distances = _bfs_floor(cells, width, height, up_at)
    down_room = first
    best = -1
    for room in rooms:
        found = distances.get(room.centre(), -1)
        if found > best:
            best = found
            down_room = room
    down_at = down_room.centre()
    if down_at == up_at:
        below = (down_at[0], down_at[1] + 1)
        down_at = below if down_room.contains(below) else (down_at[0], down_at[1] - 1)

    features = tuple(doors) + (
        GeneratedFeature(id="stairs-up-1", kind="stairs_up", at=up_at),
        GeneratedFeature(id="spawn-party", kind="spawn", at=spawn_at, team="party"),
        GeneratedFeature(id="stairs-down-1", kind="stairs_down", at=down_at),
    )
    return GeneratedMap(
        width=width, height=height,
        cells=tuple(tuple(row) for row in cells),
        features=features,
    )


_CARDINAL_STEPS: tuple[tuple[int, int], ...] = ((0, -1), (0, 1), (-1, 0), (1, 0))


def _bfs_floor(
    cells: list[list[str]], width: int, height: int, start: Square
) -> dict[Square, int]:
    """Breadth-first distances over floor cells, 4-connected, fixed step order."""
    distances: dict[Square, int] = {start: 0}
    queue: deque[Square] = deque([start])
    while queue:
        x, y = queue.popleft()
        step = distances[(x, y)] + 1
        for dx, dy in _CARDINAL_STEPS:
            nx, ny = x + dx, y + dy
            if not (0 <= nx < width and 0 <= ny < height):
                continue
            if cells[ny][nx] != "floor" or (nx, ny) in distances:
                continue
            distances[(nx, ny)] = step
            queue.append((nx, ny))
    return distances
