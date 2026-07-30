"""Map generators: determinism, structural guarantees, and document encoding.

Two kinds of pin live here. The *golden snapshot* and the *float-determinism
canary* are declared reproducibility contracts: if either fails, the same seed
no longer reproduces the same map for someone re-opening a campaign, and that
is a break to be declared, never absorbed. The property tests are the ones
that hold under any refactor: borders, reachability, region counts, and that
every emitted kind is real terrain.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from random import Random

import pytest

from fivee_sim.kernel.grid import TERRAIN, Square
from fivee_sim.kernel.mapgen import (
    CaveParams,
    DungeonParams,
    GeneratedMap,
    OverlandParams,
    generate_caves,
    generate_dungeon,
    generate_overland,
)
from fivee_sim.maps import (
    DEFAULT_LEGEND,
    GENERATED_SOURCE,
    as_payload,
    document_from,
    parse_document,
    serialize,
    validate_document,
)

GLYPH_OF = {kind: glyph for glyph, kind in DEFAULT_LEGEND.items()}


def rows_of(generated: GeneratedMap) -> list[str]:
    return ["".join(GLYPH_OF[kind] for kind in row) for row in generated.cells]


def floor_cells(generated: GeneratedMap) -> set[Square]:
    return {
        (x, y)
        for y, row in enumerate(generated.cells)
        for x, kind in enumerate(row)
        if kind == "floor"
    }


def regions_of(cells: set[Square]) -> list[set[Square]]:
    """4-connected components, for the reachability and cave-region checks."""
    remaining = set(cells)
    found: list[set[Square]] = []
    while remaining:
        start = min(remaining)
        component = {start}
        queue: deque[Square] = deque([start])
        remaining.discard(start)
        while queue:
            x, y = queue.popleft()
            for step in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                neighbour = (x + step[0], y + step[1])
                if neighbour in remaining:
                    remaining.discard(neighbour)
                    component.add(neighbour)
                    queue.append(neighbour)
        found.append(component)
    return found


class TestDeterminism:
    def test_same_seed_and_params_reproduce_each_generator(self) -> None:
        assert generate_dungeon(Random(99), DungeonParams()) == generate_dungeon(
            Random(99), DungeonParams()
        )
        assert generate_caves(Random(99), CaveParams()) == generate_caves(
            Random(99), CaveParams()
        )
        small = OverlandParams(width=32, height=24)
        assert generate_overland(Random(99), small) == generate_overland(Random(99), small)

    def test_a_different_seed_produces_a_different_map(self) -> None:
        assert generate_dungeon(Random(1), DungeonParams()) != generate_dungeon(
            Random(2), DungeonParams()
        )
        assert generate_caves(Random(1), CaveParams()) != generate_caves(
            Random(2), CaveParams()
        )
        small = OverlandParams(width=32, height=24)
        assert generate_overland(Random(1), small) != generate_overland(Random(2), small)

    # A declared reproducibility canary: this is the exact 24x16 dungeon seed 1
    # produces. If a change to the generator breaks it, the same seed no longer
    # rebuilds the same map for existing campaigns — declare the break, never
    # adjust the snapshot in passing.
    def test_golden_dungeon_snapshot(self) -> None:
        generated = generate_dungeon(Random(1), DungeonParams(width=24, height=16))
        assert rows_of(generated) == [
            "########################",
            "########################",
            "###########...........##",
            "###########...........##",
            "###########...........##",
            "##....#####...........##",
            "##....#####...........##",
            "##....#####...........##",
            "##....#####...........##",
            "##....................##",
            "##....#####...........##",
            "##....#####...........##",
            "##....#####...........##",
            "########################",
            "########################",
            "########################",
        ]
        by_id = {feature.id: feature for feature in generated.features}
        assert list(by_id) == [
            "door-1", "door-2", "stairs-up-1", "spawn-party", "stairs-down-1",
        ]
        assert (by_id["door-1"].at, by_id["door-1"].orientation) == ((6, 9), "vertical")
        assert (by_id["door-2"].at, by_id["door-2"].state) == ((10, 9), "closed")
        assert by_id["stairs-up-1"].at == (4, 9)
        assert (by_id["spawn-party"].at, by_id["spawn-party"].team) == ((5, 9), "party")
        assert by_id["stairs-down-1"].at == (16, 7)

    # The float-determinism canary: value noise uses only add, subtract,
    # multiply, divide, and smoothstep, so this hash is identical on every
    # platform. A mismatch means a float operation crept in that is not —
    # declare the break if the change is deliberate.
    def test_overland_exact_hash(self) -> None:
        generated = generate_overland(Random(7), OverlandParams(width=32, height=24))
        joined = "\n".join(rows_of(generated))
        assert (
            hashlib.sha256(joined.encode("utf-8")).hexdigest()
            == "5e2222916774e745ec1258373c909104b0846729f932f6abbad5ae635f3fa0a2"
        )


class TestEmittedKinds:
    def test_every_kind_from_every_generator_is_builtin_terrain(self) -> None:
        outputs = (
            generate_dungeon(Random(11), DungeonParams()),
            generate_caves(Random(11), CaveParams()),
            generate_overland(Random(11), OverlandParams(width=48, height=32)),
        )
        for generated in outputs:
            for row in generated.cells:
                for kind in row:
                    assert kind in TERRAIN


class TestDungeonProperties:
    @pytest.mark.parametrize("seed", [1, 5, 42])
    def test_the_border_is_solid_wall(self, seed: int) -> None:
        generated = generate_dungeon(Random(seed), DungeonParams())
        assert set(generated.cells[0]) == {"wall"}
        assert set(generated.cells[-1]) == {"wall"}
        for row in generated.cells:
            assert row[0] == "wall" and row[-1] == "wall"

    @pytest.mark.parametrize("seed", [1, 5, 42])
    def test_every_floor_cell_is_reachable_from_the_spawn(self, seed: int) -> None:
        generated = generate_dungeon(Random(seed), DungeonParams())
        floors = floor_cells(generated)
        spawn = next(f for f in generated.features if f.kind == "spawn")
        assert spawn.at in floors
        components = regions_of(floors)
        assert len(components) == 1

    @pytest.mark.parametrize("seed", [1, 5, 42])
    def test_doors_sit_in_doorways(self, seed: int) -> None:
        generated = generate_dungeon(Random(seed), DungeonParams())
        floors = floor_cells(generated)
        doors = [f for f in generated.features if f.kind == "door"]
        assert doors, "the default door_chance should place at least one door"
        for door in doors:
            assert door.at in floors  # the feature supplies the blocking
            assert door.orientation in ("horizontal", "vertical")
            assert door.state == "closed"
            x, y = door.at
            walls = sum(
                1
                for nx, ny in ((x, y - 1), (x, y + 1), (x - 1, y), (x + 1, y))
                if generated.cells[ny][nx] == "wall"
            )
            assert walls >= 2

    def test_stairs_land_apart(self) -> None:
        generated = generate_dungeon(Random(5), DungeonParams())
        by_kind = {f.kind: f for f in generated.features if f.kind.startswith("stairs")}
        assert set(by_kind) == {"stairs_up", "stairs_down"}
        assert by_kind["stairs_up"].at != by_kind["stairs_down"].at

    def test_impossible_dimensions_are_refused(self) -> None:
        with pytest.raises(ValueError, match="at least"):
            generate_dungeon(Random(1), DungeonParams(width=6, height=6))


class TestCaveProperties:
    @pytest.mark.parametrize("seed", [3, 9, 21])
    def test_exactly_one_floor_region_at_or_over_the_minimum(self, seed: int) -> None:
        params = CaveParams()
        generated = generate_caves(Random(seed), params)
        components = regions_of(floor_cells(generated))
        assert len(components) == 1
        assert len(components[0]) >= params.min_region

    def test_without_joining_only_the_largest_region_survives(self) -> None:
        generated = generate_caves(Random(3), CaveParams(connect_regions=False))
        assert len(regions_of(floor_cells(generated))) == 1

    def test_the_border_is_solid_wall(self) -> None:
        generated = generate_caves(Random(3), CaveParams())
        for row in generated.cells:
            assert row[0] == "wall" and row[-1] == "wall"
        assert set(generated.cells[0]) == {"wall"}
        assert set(generated.cells[-1]) == {"wall"}

    def test_the_spawn_is_the_first_floor_cell_in_scan_order(self) -> None:
        generated = generate_caves(Random(3), CaveParams())
        spawn = next(f for f in generated.features if f.kind == "spawn")
        first = min(floor_cells(generated), key=lambda square: (square[1], square[0]))
        assert spawn.at == first


class TestOverlandProperties:
    def test_water_grows_with_the_water_level(self) -> None:
        counts = []
        for level in (0.2, 0.3, 0.45):
            params = OverlandParams(width=32, height=24, water_level=level)
            generated = generate_overland(Random(7), params)
            counts.append(sum(row.count("water") for row in generated.cells))
        assert counts[0] <= counts[1] <= counts[2]
        assert counts[0] < counts[2]

    def test_the_spawn_sits_on_plain(self) -> None:
        generated = generate_overland(Random(7), OverlandParams(width=32, height=24))
        spawn = next(f for f in generated.features if f.kind == "spawn")
        assert generated.cells[spawn.at[1]][spawn.at[0]] == "plain"

    def test_disordered_bands_are_refused(self) -> None:
        with pytest.raises(ValueError, match="ordered"):
            generate_overland(
                Random(1), OverlandParams(water_level=0.9, hill_level=0.2)
            )


class TestDocumentFrom:
    def test_each_generator_round_trips_with_zero_diagnostics(self) -> None:
        from fivee_sim.kernel.grid import TERRAIN as table

        cases = (
            ("dungeon", generate_dungeon(Random(4), DungeonParams(width=24, height=16)),
             DungeonParams(width=24, height=16)),
            ("caves", generate_caves(Random(4), CaveParams(width=24, height=16)),
             CaveParams(width=24, height=16)),
            ("overland", generate_overland(Random(4), OverlandParams(width=24, height=16)),
             OverlandParams(width=24, height=16)),
        )
        for generator, generated, params in cases:
            doc = document_from(
                generated, name=f"{generator}-4", generator=generator, seed=4, params=params
            )
            payload = as_payload(doc)
            assert validate_document(payload, source=generator, terrain=table) == []
            parsed = parse_document(
                json.loads(serialize(doc)), source=generator, terrain=table
            )
            assert parsed == doc

    def test_provenance_records_the_full_resolution(self) -> None:
        params = DungeonParams(width=24, height=16)
        generated = generate_dungeon(Random(4), params)
        doc = document_from(generated, name="d", generator="dungeon", seed=4, params=params)
        assert doc.provenance.generator == "dungeon"
        assert doc.provenance.seed == 4
        assert doc.provenance.edited is False
        assert doc.provenance.source == GENERATED_SOURCE
        assert dict(doc.provenance.params) == {
            "width": 24, "height": 16, "min_room": 4, "max_room": 12, "min_leaf": 8,
            "split_bias": 1.25, "door_chance": 0.75, "extra_connections": 2,
        }

    def test_an_unencodable_kind_is_refused(self) -> None:
        from fivee_sim.maps import MapError

        generated = GeneratedMap(
            width=1, height=1, cells=(("normal",),), features=()
        )
        with pytest.raises(MapError, match="no glyph"):
            document_from(generated, name="odd", generator="hand", seed=0, params={})
