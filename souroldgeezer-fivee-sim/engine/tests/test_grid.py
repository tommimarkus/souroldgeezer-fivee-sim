"""Grid tests: distance, sight, cover, area templates, pathfinding.

Geometry goldens rather than restated formulas: each scenario is small enough to
check by drawing it, and the assertions pin the exact squares so a refactor that
shifts a raster or a tie-break by one cell fails loudly. The sight and cover
policies documented on the module — face grazes and corner threads never block,
the seam inside a solid wall always does — are pinned here too, because they are
policy, not derivation.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from fivee_sim.kernel.grid import (
    CLIMB_FEET,
    SLOPE_DIFFICULT_FEET,
    TERRAIN,
    TERRAIN_FLAGS,
    CoverGrade,
    DiagonalRule,
    Square,
    UnknownTerrain,
    cone_squares,
    cover_ac_bonus,
    cover_between,
    cube_squares,
    distance_feet,
    find_path,
    has_line_of_sight,
    line_squares,
    sphere_squares,
    square_center,
    step_cost_feet,
    terrain_effect_of,
    to_square,
)


def opaque_from(walls: frozenset[Square]) -> Callable[[Square], bool]:
    return lambda square: square in walls


def cover_from(walls: frozenset[Square], soft: dict[Square, int] | None = None) -> (
    Callable[[Square], int]
):
    """Walls grant TOTAL, exactly as an encounter composing terrain would map them."""
    granted = dict(soft or {})

    def cover_of(square: Square) -> int:
        if square in walls:
            return int(CoverGrade.TOTAL)
        return granted.get(square, 0)

    return cover_of


class TestDistance:
    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            ((0, 0), (30, 0), 30),
            ((0, 0), (15, 20), 20),
            ((0, 0), (20, 20), 20),  # a diagonal costs its longer axis alone
            ((0, 0), (3, 4), 4),  # off the 5-foot lattice stays exact
            ((10, 10), (-10, -5), 20),
        ],
    )
    def test_five_five_five_is_chebyshev(self, a: tuple[int, int], b: tuple[int, int],
                                         expected: int) -> None:
        assert distance_feet(a, b) == expected
        assert distance_feet(b, a) == expected

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            ((0, 0), (30, 0), 30),  # no diagonal, no surcharge
            ((0, 0), (20, 20), 30),
            ((0, 0), (15, 20), 27),
            ((0, 0), (30, 10), 35),
            ((0, 0), (5, 5), 7),  # dmax + dmin // 2, computed in feet
            ((0, 0), (3, 4), 5),  # non-multiple-of-5 positions stay exact
            ((10, 10), (-10, -5), 27),
        ],
    )
    def test_five_ten_five_charges_half_the_short_axis(
        self, a: tuple[int, int], b: tuple[int, int], expected: int
    ) -> None:
        assert distance_feet(a, b, DiagonalRule.FIVE_TEN_FIVE) == expected
        assert distance_feet(b, a, DiagonalRule.FIVE_TEN_FIVE) == expected

    def test_the_srd_rule_is_the_default(self) -> None:
        assert distance_feet((0, 0), (20, 20)) == distance_feet(
            (0, 0), (20, 20), DiagonalRule.FIVE_FIVE_FIVE
        )

    def test_square_conversions_round_trip(self) -> None:
        assert to_square((12, 7)) == (2, 1)
        assert square_center((2, 1)) == (10, 5)
        assert to_square(square_center((3, 8))) == (3, 8)


class TestLineOfSight:
    def test_a_solid_wall_blocks(self) -> None:
        walls = frozenset({(2, 0), (2, 1), (2, 2)})
        assert not has_line_of_sight((0, 0), (4, 2), opaque=opaque_from(walls))

    def test_a_solid_wall_blocks_a_target_directly_behind_it(self) -> None:
        # The sight line here runs exactly along the seam between two stacked
        # wall squares. The seam inside a solid wall blocks — only the *outer*
        # face of a wall may be grazed.
        walls = frozenset({(2, y) for y in range(-1, 3)})
        assert not has_line_of_sight((0, 0), (4, 0), opaque=opaque_from(walls))

    def test_a_doorway_gap_sees_through(self) -> None:
        walls = frozenset({(2, 0), (2, 2)})  # (2, 1) is the doorway
        assert has_line_of_sight((0, 0), (4, 2), opaque=opaque_from(walls))
        assert has_line_of_sight((0, 0), (4, 0), opaque=opaque_from(walls))

    def test_a_single_pillar_does_not_block_sight(self) -> None:
        # Corners see past one square of wall; it grants cover instead.
        assert has_line_of_sight((0, 0), (4, 0), opaque=opaque_from(frozenset({(2, 0)})))

    def test_the_corner_between_diagonal_walls_does_not_block(self) -> None:
        # Policy, pinned: a sight line may thread the exact corner point where
        # two opaque squares touch only diagonally.
        pinch = frozenset({(1, 0), (0, 1), (2, 1), (1, 2)})
        assert has_line_of_sight((0, 0), (2, 2), opaque=opaque_from(pinch))
        # Sealing the passage square shuts it.
        assert not has_line_of_sight(
            (0, 0), (2, 2), opaque=opaque_from(pinch | {(1, 1)})
        )

    def test_same_square_and_adjacent_always_see(self) -> None:
        everything = opaque_from(frozenset())
        assert has_line_of_sight((3, 3), (3, 3), opaque=everything)
        walls = frozenset({(0, 0), (1, 1)})
        # The endpoints' own squares are never treated as blockers.
        assert has_line_of_sight((0, 0), (1, 1), opaque=opaque_from(walls))

    def test_sight_is_symmetric(self) -> None:
        walls = frozenset({(2, 0), (2, 1), (2, 2), (4, 4)})
        for a in [(0, 0), (0, 2), (4, 2), (5, 5)]:
            for b in [(4, 0), (4, 2), (0, 4), (3, 3)]:
                assert has_line_of_sight(a, b, opaque=opaque_from(walls)) == (
                    has_line_of_sight(b, a, opaque=opaque_from(walls))
                )


class TestCover:
    def test_open_ground_is_no_cover(self) -> None:
        assert cover_between(
            (0, 0), (4, 0), cover_of=cover_from(frozenset())
        ) is CoverGrade.NONE

    def test_a_single_pillar_grants_half_cover(self) -> None:
        assert cover_between(
            (0, 0), (4, 0), cover_of=cover_from(frozenset({(2, 0)}))
        ) is CoverGrade.HALF

    def test_the_end_of_a_wall_grants_three_quarters(self) -> None:
        # Attacker peeks past the end of a wall at a target tucked behind it:
        # three of four corner lines are blocked from the best corner.
        walls = frozenset({(2, 1), (2, 2)})
        assert cover_between(
            (0, 0), (4, 2), cover_of=cover_from(walls)
        ) is CoverGrade.THREE_QUARTERS

    def test_a_sealed_target_has_total_cover(self) -> None:
        box = frozenset({(4, 4), (5, 4), (6, 4), (4, 5), (6, 5), (4, 6), (5, 6), (6, 6)})
        assert cover_between(
            (0, 0), (5, 5), cover_of=cover_from(box)
        ) is CoverGrade.TOTAL

    def test_an_intervening_creature_grants_half_cover(self) -> None:
        grade = cover_between(
            (0, 0), (4, 0),
            cover_of=cover_from(frozenset()),
            occupied=frozenset({(2, 0)}),
        )
        assert grade is CoverGrade.HALF

    def test_creatures_alone_never_grant_more_than_half(self) -> None:
        # A wall of bodies blocks every corner line, and it is still half cover.
        grade = cover_between(
            (0, 0), (4, 0),
            cover_of=cover_from(frozenset()),
            occupied=frozenset({(2, -1), (2, 0), (2, 1)}),
        )
        assert grade is CoverGrade.HALF

    def test_a_half_cover_screen_never_grants_more_than_half(self) -> None:
        # Terrain contributes at the grade it carries: a cover-1 screen blocking
        # all four lines is still half cover, not total.
        soft = {(2, -1): 1, (2, 0): 1, (2, 1): 1}
        grade = cover_between(
            (0, 0), (4, 0), cover_of=cover_from(frozenset(), soft)
        )
        assert grade is CoverGrade.HALF

    def test_total_cover_coincides_exactly_with_lost_sight(self) -> None:
        # The invariant: with walls — where opacity and full cover coincide —
        # TOTAL is equivalent to has_line_of_sight being false.
        box = frozenset({(4, 4), (5, 4), (6, 4), (4, 5), (6, 5), (4, 6), (5, 6), (6, 6)})
        with_door = box - {(6, 5)}
        for walls in (box, with_door):
            for attacker in [(0, 0), (0, 5), (5, 0), (8, 8), (5, 2), (8, 5)]:
                total = cover_between(
                    attacker, (5, 5), cover_of=cover_from(walls)
                ) is CoverGrade.TOTAL
                sees = has_line_of_sight(attacker, (5, 5), opaque=opaque_from(walls))
                assert total == (not sees), (walls is box, attacker)

    def test_the_ac_bonus_ladder(self) -> None:
        assert cover_ac_bonus(CoverGrade.NONE) == 0
        assert cover_ac_bonus(CoverGrade.HALF) == 2
        assert cover_ac_bonus(CoverGrade.THREE_QUARTERS) == 5

    def test_total_cover_has_no_ac_bonus_to_ask_for(self) -> None:
        with pytest.raises(ValueError, match="cannot be targeted"):
            cover_ac_bonus(CoverGrade.TOTAL)


class TestAoeTemplates:
    def test_a_20_foot_sphere_is_a_9_by_9_disc(self) -> None:
        expected = frozenset((x, y) for x in range(-4, 5) for y in range(-4, 5))
        assert sphere_squares((0, 0), 20) == expected

    def test_the_diagonal_knob_governs_areas_too(self) -> None:
        disc = sphere_squares((0, 0), 20, rule=DiagonalRule.FIVE_TEN_FIVE)
        assert len(disc) == 49
        assert (4, 0) in disc and (3, 2) in disc
        assert (3, 3) not in disc and (4, 1) not in disc

    def test_a_cardinal_cone(self) -> None:
        assert cone_squares((0, 0), (1, 0), 15) == frozenset({
            (1, 0), (2, -1), (2, 0), (2, 1), (3, -1), (3, 0), (3, 1),
        })

    def test_a_diagonal_cone(self) -> None:
        assert cone_squares((0, 0), (1, 1), 15) == frozenset({
            (1, 0), (0, 1), (1, 1),
            (2, 1), (1, 2), (2, 2),
            (3, 1), (1, 3), (3, 2), (2, 3), (3, 3),
        })

    def test_a_30_foot_cone_grows_with_its_range(self) -> None:
        cone = cone_squares((0, 0), (0, 1), 30)
        assert len(cone) == 24
        assert (0, 1) in cone and (3, 6) in cone and (-3, 6) in cone
        assert (4, 6) not in cone and (0, 0) not in cone

    @pytest.mark.parametrize("length_feet", [15, 30])
    def test_every_aim_of_a_kind_rasters_the_same_size(self, length_feet: int) -> None:
        cardinals = {len(cone_squares((0, 0), d, length_feet))
                     for d in [(1, 0), (-1, 0), (0, 1), (0, -1)]}
        diagonals = {len(cone_squares((0, 0), d, length_feet))
                     for d in [(1, 1), (1, -1), (-1, 1), (-1, -1)]}
        assert len(cardinals) == 1
        assert len(diagonals) == 1

    def test_a_bad_cone_direction_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unit offsets"):
            cone_squares((0, 0), (2, 0), 15)

    def test_a_line_walks_the_supercover(self) -> None:
        assert line_squares((0, 0), (4, 1), 30) == frozenset({
            (1, 0), (2, 0), (2, 1), (3, 1), (4, 1),
        })

    def test_a_line_is_truncated_at_its_length(self) -> None:
        assert line_squares((0, 0), (4, 1), 15) == frozenset({(1, 0), (2, 0), (2, 1)})

    def test_a_diagonal_line_counts_its_grazed_corners(self) -> None:
        # Supercover policy, pinned: squares grazed at an exact corner crossing
        # are part of the line, in walk order, and count against its length.
        assert line_squares((0, 0), (3, 3), 15) == frozenset({(1, 0), (0, 1), (1, 1)})

    def test_a_cube_is_a_block_from_its_minimum_corner(self) -> None:
        assert cube_squares((2, 3), 15) == frozenset({
            (2, 3), (2, 4), (2, 5), (3, 3), (3, 4), (3, 5), (4, 3), (4, 4), (4, 5),
        })


def flat(_origin: Square, _step_to: Square, doubled: bool) -> int:
    """Ordinary ground everywhere, on level ground."""
    return 10 if doubled else 5


def stepping(
    entry_cost: Callable[[Square], int | None],
) -> Callable[[Square, Square, bool], int | None]:
    """A step composer over a per-square cost: level ground of varying going."""

    def step_cost(_origin: Square, step_to: Square, doubled: bool) -> int | None:
        cost = entry_cost(step_to)
        if cost is None:
            return None
        return cost * 2 if doubled else cost

    return step_cost


def rising(heights: dict[Square, int]) -> Callable[[Square, Square, bool], int | None]:
    """Ordinary ground at the heights given, everything unnamed at zero."""

    def step_cost(origin: Square, step_to: Square, doubled: bool) -> int | None:
        return step_cost_feet(
            terrain_effect_of("normal"),
            heights.get(step_to, 0) - heights.get(origin, 0),
            doubled_diagonal=doubled,
        )

    return step_cost


class TestStepCost:
    """The three bands of :func:`step_cost_feet`, and the boundaries between them."""

    def test_level_ground_costs_what_the_terrain_costs(self) -> None:
        assert step_cost_feet(terrain_effect_of("normal"), 0) == 5
        assert step_cost_feet(terrain_effect_of("difficult"), 0) == 10
        assert step_cost_feet(terrain_effect_of("wall"), 0) is None

    def test_a_gentle_grade_is_free(self) -> None:
        gentle = SLOPE_DIFFICULT_FEET - 1
        assert step_cost_feet(terrain_effect_of("normal"), gentle) == 5
        assert step_cost_feet(terrain_effect_of("difficult"), gentle) == 10

    def test_a_slope_is_difficult_terrain_but_never_twice(self) -> None:
        # SRD 5.2: a slope of 20 degrees or more is Difficult Terrain, and
        # Difficult Terrain "isn't cumulative" — so a slope through undergrowth
        # is doubled once, not quadrupled.
        assert step_cost_feet(terrain_effect_of("normal"), SLOPE_DIFFICULT_FEET) == 10
        assert step_cost_feet(terrain_effect_of("normal"), CLIMB_FEET) == 10
        assert step_cost_feet(terrain_effect_of("difficult"), CLIMB_FEET) == 10

    def test_a_climb_costs_an_extra_foot_per_foot(self) -> None:
        # SRD 5.2, "Climbing": each foot of movement costs 1 extra foot, 2 extra
        # in Difficult Terrain — charged on top of the step into the square.
        assert step_cost_feet(terrain_effect_of("normal"), 10) == 5 + 20
        assert step_cost_feet(terrain_effect_of("difficult"), 10) == 10 + 30

    def test_the_cost_jumps_where_the_slope_becomes_a_climb(self) -> None:
        # Policy, not an accident: there is no graduated scale in the SRD, so
        # ruling a boundary at all buys a step at it.
        assert step_cost_feet(terrain_effect_of("normal"), CLIMB_FEET) == 10
        assert step_cost_feet(terrain_effect_of("normal"), CLIMB_FEET + 1) == 17

    def test_climbing_down_costs_what_climbing_up_costs(self) -> None:
        for rise in (SLOPE_DIFFICULT_FEET, CLIMB_FEET, 10, 40):
            up = step_cost_feet(terrain_effect_of("normal"), rise)
            assert up == step_cost_feet(terrain_effect_of("normal"), -rise)

    def test_the_variant_diagonal_doubles_the_travel_not_the_climb(self) -> None:
        # 5-10-5 lengthens diagonal travel; a climb is not travelled diagonally.
        assert step_cost_feet(terrain_effect_of("normal"), 0, doubled_diagonal=True) == 10
        assert step_cost_feet(terrain_effect_of("normal"), 10, doubled_diagonal=True) == (
            10 + 20
        )
        assert step_cost_feet(terrain_effect_of("wall"), 0, doubled_diagonal=True) is None


class TestPathfindingOverHeight:
    def test_a_route_prefers_the_long_ramp_to_the_short_cliff(self) -> None:
        # A 20-foot plateau on the right half of a 3-row map, reached directly at
        # (2, 1) or up a ramp that climbs 5 feet a square along the top row.
        heights = {
            (2, 0): 5, (3, 0): 10, (4, 0): 15, (5, 0): 20,
            (2, 1): 20, (3, 1): 20, (4, 1): 20, (5, 1): 20,
        }
        path = find_path((0, 1), (5, 1), step_cost=rising(heights), bounds=(6, 3))
        assert path is not None
        # Every step of the ramp is a slope, so difficult terrain: 10 feet each.
        assert path.squares == ((0, 1), (1, 0), (2, 0), (3, 0), (4, 0), (5, 1))
        assert path.cost_feet == 5 + 10 * 4
        # Straight at the cliff instead: 5 onto level ground, then a 20-foot climb
        # at 1 extra foot per foot, then three level squares.
        assert 5 + (5 + 40) + 5 * 3 > path.cost_feet

    def test_a_cliff_with_no_way_round_is_climbed_and_charged(self) -> None:
        heights = {(2, 0): 20, (3, 0): 20, (4, 0): 20}
        path = find_path((0, 0), (4, 0), step_cost=rising(heights), bounds=(5, 1))
        assert path is not None
        assert path.cost_feet == 5 + (5 + 40) + 5 + 5

    def test_the_descent_is_charged_like_the_ascent(self) -> None:
        heights = {(0, 0): 20, (1, 0): 20}
        down = find_path((0, 0), (2, 0), step_cost=rising(heights), bounds=(3, 1))
        up = find_path((2, 0), (0, 0), step_cost=rising(heights), bounds=(3, 1))
        assert down is not None and up is not None
        assert down.cost_feet == up.cost_feet == 5 + (5 + 40)

    def test_impassable_ground_still_stops_a_climb(self) -> None:
        def step_cost(origin: Square, step_to: Square, doubled: bool) -> int | None:
            kind = "wall" if step_to[0] == 1 else "normal"
            return step_cost_feet(terrain_effect_of(kind), 0, doubled_diagonal=doubled)

        assert find_path((0, 0), (2, 0), step_cost=step_cost, bounds=(3, 1)) is None


class TestPathfinding:
    def test_a_straight_run(self) -> None:
        path = find_path((0, 0), (4, 0), step_cost=flat, bounds=(6, 6))
        assert path is not None
        assert path.squares == ((0, 0), (1, 0), (2, 0), (3, 0), (4, 0))
        assert path.cost_feet == 20

    def test_a_wall_is_walked_around(self) -> None:
        walls = {(2, 0), (2, 1), (2, 2)}

        def entry(square: Square) -> int | None:
            return None if square in walls else 5

        path = find_path((0, 0), (4, 0), step_cost=stepping(entry), bounds=(6, 6))
        assert path is not None
        assert path.cost_feet == 30
        assert not set(path.squares) & walls

    def test_difficult_ground_doubles_the_cost(self) -> None:
        def entry(square: Square) -> int:
            return 10 if square == (2, 0) else 5

        # A one-row corridor: no way around, so the doubled square must be paid.
        path = find_path((0, 0), (4, 0), step_cost=stepping(entry), bounds=(6, 1))
        assert path is not None
        assert path.squares == ((0, 0), (1, 0), (2, 0), (3, 0), (4, 0))
        assert path.cost_feet == 25

    def test_blocked_squares_are_respected(self) -> None:
        blocked = frozenset({(2, 0), (2, 1), (2, 2)})
        path = find_path((0, 0), (4, 0), step_cost=flat, bounds=(6, 6), blocked=blocked)
        assert path is not None
        assert not set(path.squares) & blocked
        assert path.cost_feet == 30

    def test_the_same_inputs_give_the_same_path(self) -> None:
        walls = {(2, 0), (2, 1), (2, 2)}

        def entry(square: Square) -> int | None:
            return None if square in walls else 5

        first = find_path((0, 0), (5, 3), step_cost=stepping(entry), bounds=(8, 8),
                          rule=DiagonalRule.FIVE_TEN_FIVE)
        second = find_path((0, 0), (5, 3), step_cost=stepping(entry), bounds=(8, 8),
                           rule=DiagonalRule.FIVE_TEN_FIVE)
        assert first == second
        assert first is not None
        assert first.cost_feet == 35

    def test_equal_cost_routes_break_ties_lexicographically(self) -> None:
        # (0,0) -> (2,0) costs 10 via (1,0) or (1,1); the heap orders equal-cost
        # candidates by square, so (1,0) wins — pinned, never insertion order.
        path = find_path((0, 0), (2, 0), step_cost=flat, bounds=(3, 3))
        assert path is not None
        assert path.squares == ((0, 0), (1, 0), (2, 0))
        assert path.cost_feet == 10

    def test_stop_adjacent_halts_beside_the_goal(self) -> None:
        path = find_path((0, 0), (4, 0), step_cost=flat, bounds=(6, 6),
                         stop_adjacent=True)
        assert path is not None
        assert path.squares == ((0, 0), (1, 0), (2, 0), (3, 0))
        assert path.cost_feet == 15

    def test_stop_adjacent_from_next_door_is_already_there(self) -> None:
        path = find_path((3, 0), (4, 0), step_cost=flat, bounds=(6, 6),
                         stop_adjacent=True)
        assert path == find_path((3, 0), (3, 0), step_cost=flat, bounds=(6, 6))
        assert path is not None
        assert path.squares == ((3, 0),)
        assert path.cost_feet == 0

    def test_max_cost_bounds_the_budget(self) -> None:
        assert find_path((0, 0), (4, 0), step_cost=flat, bounds=(6, 6),
                         max_cost=15) is None
        path = find_path((0, 0), (4, 0), step_cost=flat, bounds=(6, 6), max_cost=20)
        assert path is not None
        assert path.cost_feet == 20

    def test_the_variant_diagonal_rule_changes_the_route(self) -> None:
        # Difficult ground on the destination: under 5-5-5 the two-diagonal
        # route wins outright; under 5-10-5 the second diagonal into difficult
        # ground would cost 20, so the cheap path takes an extra square.
        def entry(square: Square) -> int:
            return 10 if square == (2, 2) else 5

        srd = find_path((0, 0), (2, 2), step_cost=stepping(entry), bounds=(4, 4))
        assert srd is not None
        assert srd.squares == ((0, 0), (1, 1), (2, 2))
        assert srd.cost_feet == 15

        variant = find_path((0, 0), (2, 2), step_cost=stepping(entry), bounds=(4, 4),
                            rule=DiagonalRule.FIVE_TEN_FIVE)
        assert variant is not None
        assert variant.squares == ((0, 0), (1, 1), (1, 2), (2, 2))
        assert variant.cost_feet == 20

    def test_diagonal_parity_is_paid_on_a_pure_diagonal(self) -> None:
        path = find_path((0, 0), (4, 4), step_cost=flat, bounds=(6, 6),
                         rule=DiagonalRule.FIVE_TEN_FIVE)
        assert path is not None
        assert path.squares == ((0, 0), (1, 1), (2, 2), (3, 3), (4, 4))
        assert path.cost_feet == 30  # 5 + 10 + 5 + 10

    def test_an_unreachable_goal_is_none(self) -> None:
        walls = {(1, y) for y in range(4)}

        def entry(square: Square) -> int | None:
            return None if square in walls else 5

        assert find_path((0, 0), (3, 0), step_cost=stepping(entry), bounds=(4, 4)) is None

    def test_out_of_bounds_endpoints_are_none(self) -> None:
        assert find_path((0, 0), (9, 9), step_cost=flat, bounds=(4, 4)) is None
        assert find_path((-1, 0), (2, 2), step_cost=flat, bounds=(4, 4)) is None


class TestTerrainTable:
    def test_the_flags_are_derived_from_the_dataclass(self) -> None:
        assert TERRAIN_FLAGS == (
            "move_cost_multiplier",
            "passable",
            "opaque",
            "underwater",
            "cover",
        )

    def test_the_dungeon_kinds(self) -> None:
        assert terrain_effect_of("normal") == terrain_effect_of("floor")
        assert terrain_effect_of("door-open") == terrain_effect_of("normal")
        wall = terrain_effect_of("wall")
        assert not wall.passable and wall.opaque
        assert terrain_effect_of("door-closed") == wall
        assert terrain_effect_of("difficult").move_cost_multiplier == 2
        assert terrain_effect_of("half-cover").cover == int(CoverGrade.HALF)
        assert terrain_effect_of("three-quarters-cover").cover == (
            int(CoverGrade.THREE_QUARTERS)
        )

    def test_the_overland_kinds(self) -> None:
        assert terrain_effect_of("plain") == terrain_effect_of("normal")
        assert terrain_effect_of("water").move_cost_multiplier == 2
        forest = terrain_effect_of("forest")
        assert forest.move_cost_multiplier == 2 and forest.cover == int(CoverGrade.HALF)
        assert terrain_effect_of("hill").move_cost_multiplier == 2
        mountain = terrain_effect_of("mountain")
        # Impassable but not opaque: you cannot enter it, you can see over it.
        assert not mountain.passable and not mountain.opaque

    def test_an_unknown_kind_reports_what_the_table_defines(self) -> None:
        with pytest.raises(UnknownTerrain, match="difficult"):
            terrain_effect_of("swamp")
        with pytest.raises(UnknownTerrain, match="swamp"):
            terrain_effect_of("swamp")

    def test_a_custom_table_is_consulted_instead_of_the_builtin(self) -> None:
        table = {"vale-mire": terrain_effect_of("difficult")}
        assert terrain_effect_of("vale-mire", table).move_cost_multiplier == 2
        with pytest.raises(UnknownTerrain, match="vale-mire"):
            terrain_effect_of("normal", table)
        assert "normal" in TERRAIN
