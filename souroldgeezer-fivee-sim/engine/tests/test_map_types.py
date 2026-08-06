"""The map's types, and the questions they answer about themselves.

Every document here is built by hand rather than parsed, and that is the point
of the file as much as of the module: :mod:`fivee_sim.map_types` reads no file,
so a test that had to call ``parse_document`` to get a ``MapDocument`` would be
proving the parser works rather than proving the types stand alone.

What is under test is the handful of derivations the tree owes its readers —
which features are fixtures the fight owns, which storey one stands on, what a
square is, and what a stairway, a sight link and a lamp fan out to. Each of them
existed already, spelled structurally inside a bridge that turned every document
into a second map model before a fight could read it; naming them here is what
let that bridge and that second model go, and it is why a fight and the map
service cannot disagree about a square without one of these cases failing.
"""

from __future__ import annotations

import pytest

from fivee_sim.kernel.rules import Ability
from fivee_sim.map_types import (
    GROUND_LEVEL,
    FeatureCheck,
    HeightPair,
    MapDocument,
    MapElevation,
    MapFeatureRecord,
    MapGrid,
    MapLevel,
    MapLight,
    MapOverlayRecord,
    MapProvenance,
    SquareClaim,
    TerrainPair,
)

FIXTURE = "Authored for the test suite; 5E-compatible original content"

LEGEND = {".": "floor", "#": "wall", "%": "difficult"}

#: The ground: a walled chamber with one difficult square at (2, 2).
GROUND_TILES = (
    "######",
    "#....#",
    "#.%..#",
    "#....#",
    "###.##",
)
#: A gallery over the same footprint, deliberately *not* the same tiles: the
#: square that is difficult downstairs is plain floor up here, which is the
#: discriminator every "reads this level, not the ground" assertion turns on.
UPPER_TILES = (
    "#.....",
    "......",
    "......",
    "......",
    "......",
)

PROVENANCE = MapProvenance(
    generator="hand", seed=7, params={}, edited=False, source=FIXTURE
)


def level(index: int, name: str, tiles: tuple[str, ...], *features: MapFeatureRecord,
          default_height: int = 0) -> MapLevel:
    return MapLevel(
        index=index,
        name=name,
        tiles=tiles,
        features=features,
        elevation=MapElevation(default=default_height),
    )


def document(*levels: MapLevel) -> MapDocument:
    return MapDocument(
        name="test-chamber",
        grid=MapGrid(width=6, height=5),
        legend=LEGEND,
        provenance=PROVENANCE,
        levels={one.index: one for one in levels},
    )


def door(name: str = "door-1", at: tuple[int, int] = (3, 4)) -> MapFeatureRecord:
    return MapFeatureRecord(id=name, kind="door", at=at, state="closed")


def spawn(name: str = "spawn-party") -> MapFeatureRecord:
    """A feature with no ``state``: an annotation the fight never owns."""
    return MapFeatureRecord(id=name, kind="spawn", at=(1, 1), team="party")


class TestFixtures:
    """``state``, not ``kind``, is what makes a feature one a fight can operate."""

    def test_a_feature_carrying_a_state_is_a_fixture(self) -> None:
        one = level(GROUND_LEVEL, "ground", GROUND_TILES, door(), spawn())
        assert set(one.fixtures()) == {"door-1"}
        assert one.fixtures()["door-1"].kind == "door"

    def test_a_feature_without_a_state_stays_an_annotation(self) -> None:
        # A spawn hint, a drawn stairway and a brazier are all stateless, and
        # none of the three is something a fight can throw.
        stair = MapFeatureRecord(id="stair", kind="stairs_up", at=(3, 3), to_level=1)
        lamp = MapFeatureRecord(
            id="brazier", kind="light", at=(4, 1), light=MapLight(bright=20, dim=20)
        )
        one = level(GROUND_LEVEL, "ground", GROUND_TILES, spawn(), stair, lamp)
        assert one.fixtures() == {}

    def test_a_document_merges_the_fixtures_of_every_storey(self) -> None:
        doc = document(
            level(GROUND_LEVEL, "ground", GROUND_TILES, door(), spawn()),
            level(1, "gallery", UPPER_TILES, door("hatch", at=(3, 3))),
        )
        assert set(doc.fixtures()) == {"door-1", "hatch"}

    def test_the_merged_table_reads_the_ground_first(self) -> None:
        # Feature ids are unique across a whole document, so this order is not
        # a precedence rule — it is what makes the merge deterministic, and it
        # is the order ``Encounter._fixtures`` answers in for the same callers.
        doc = document(
            level(1, "gallery", UPPER_TILES, door("hatch", at=(3, 3))),
            level(GROUND_LEVEL, "ground", GROUND_TILES, door()),
        )
        assert list(doc.fixtures()) == ["door-1", "hatch"]

    def test_a_documents_fixtures_are_its_levels_fixtures_and_nothing_else(self) -> None:
        # One implementation of the state gate, asked two ways. A second copy of
        # `state is None` is how a document and a storey start disagreeing about
        # what a fight owns.
        doc = document(
            level(GROUND_LEVEL, "ground", GROUND_TILES, door(), spawn()),
            level(1, "gallery", UPPER_TILES, door("hatch", at=(3, 3)), spawn("spawn-up")),
        )
        merged: dict[str, MapFeatureRecord] = {}
        for index in sorted(doc.levels):
            merged.update(doc.levels[index].fixtures())
        assert dict(doc.fixtures()) == merged


class TestLevelOf:
    def test_a_fixture_names_the_storey_it_stands_on(self) -> None:
        doc = document(
            level(GROUND_LEVEL, "ground", GROUND_TILES, door()),
            level(1, "gallery", UPPER_TILES, door("hatch", at=(3, 3))),
        )
        assert doc.level_of("door-1") == GROUND_LEVEL
        assert doc.level_of("hatch") == 1

    def test_a_name_no_storey_holds_is_a_key_error(self) -> None:
        doc = document(level(GROUND_LEVEL, "ground", GROUND_TILES, door()))
        with pytest.raises(KeyError, match="portcullis"):
            doc.level_of("portcullis")

    def test_an_annotation_is_not_somewhere_a_fixture_can_be(self) -> None:
        # The question ``Encounter._fixture_level`` answers, and it answers over
        # fixtures: a spawn hint never crosses to the fight, so a fight asking
        # where it stands is asking about something that is not there.
        doc = document(level(GROUND_LEVEL, "ground", GROUND_TILES, spawn()))
        with pytest.raises(KeyError, match="spawn-party"):
            doc.level_of("spawn-party")


class TestTerrainAt:
    def test_a_square_is_its_tile_read_through_the_legend(self) -> None:
        ground = level(GROUND_LEVEL, "ground", GROUND_TILES)
        assert ground.terrain_at((0, 0), LEGEND) == "wall"
        assert ground.terrain_at((1, 1), LEGEND) == "floor"
        assert ground.terrain_at((2, 2), LEGEND) == "difficult"

    def test_a_storey_answers_from_its_own_tiles(self) -> None:
        # The whole reason this hangs off the level. (2, 2) is difficult ground
        # downstairs and plain floor in the gallery, and a reader that reached
        # for the document would give the gallery the chamber's floor.
        upper = level(1, "gallery", UPPER_TILES)
        assert upper.terrain_at((2, 2), LEGEND) == "floor"

    def test_a_square_off_the_grid_is_refused_rather_than_wrapped(self) -> None:
        # Python would read tiles[-1][-1] as the far corner and answer with a
        # straight face; a map reader asking about a square that is not there
        # has a defect, not a terrain kind.
        ground = level(GROUND_LEVEL, "ground", GROUND_TILES)
        for square in ((-1, 0), (0, -1), (6, 0), (0, 5)):
            with pytest.raises(KeyError, match=r"\("):
                ground.terrain_at(square, LEGEND)


class TestFanOuts:
    def test_a_feature_naming_another_level_becomes_a_connector(self) -> None:
        stair = MapFeatureRecord(id="stair", kind="stairs_up", at=(3, 3), to_level=1)
        one = level(GROUND_LEVEL, "ground", GROUND_TILES, stair, spawn())
        assert one.connectors() == {(3, 3): 1}

    def test_a_feature_naming_visible_levels_becomes_a_sight_link(self) -> None:
        well = MapFeatureRecord(
            id="light-well", kind="opening", at=(4, 2), sight_to_levels=(1, 2)
        )
        one = level(GROUND_LEVEL, "ground", GROUND_TILES, well)
        assert one.sight_links() == {(4, 2): frozenset({1, 2})}

    def test_a_feature_carrying_a_light_becomes_one_on_its_square(self) -> None:
        lamp = MapLight(bright=20, dim=20, color="#ffcc88")
        one = level(
            GROUND_LEVEL,
            "ground",
            GROUND_TILES,
            MapFeatureRecord(id="brazier", kind="light", at=(4, 1), light=lamp),
        )
        assert one.lights() == (((4, 1), lamp),)

    def test_two_lights_keep_the_order_the_document_wrote_them_in(self) -> None:
        # A tuple of pairs rather than a square-keyed table: two features may
        # stand on one square, and a mapping would silently keep one of them.
        first = MapLight(bright=20, dim=20)
        second = MapLight(bright=5, dim=5)
        one = level(
            GROUND_LEVEL,
            "ground",
            GROUND_TILES,
            MapFeatureRecord(id="brazier", kind="light", at=(4, 1), light=first),
            MapFeatureRecord(id="candle", kind="light", at=(4, 1), light=second),
        )
        assert one.lights() == (((4, 1), first), ((4, 1), second))

    def test_a_storey_with_none_of_the_three_fans_out_to_nothing(self) -> None:
        one = level(GROUND_LEVEL, "ground", GROUND_TILES, door(), spawn())
        assert one.connectors() == {}
        assert one.sight_links() == {}
        assert one.lights() == ()

    def test_the_three_are_read_off_every_feature_not_only_the_fixtures(self) -> None:
        # A stairway, a light well and a brazier all carry no state, so a
        # fan-out written over fixtures() would find none of them.
        one = level(
            GROUND_LEVEL,
            "ground",
            GROUND_TILES,
            MapFeatureRecord(id="stair", kind="stairs_up", at=(3, 3), to_level=1),
            MapFeatureRecord(id="well", kind="opening", at=(4, 2), sight_to_levels=(1,)),
            MapFeatureRecord(id="lamp", kind="light", at=(4, 1), light=MapLight(bright=20)),
        )
        assert one.connectors() and one.sight_links() and one.lights()


class TestOwnTerrain:
    def test_a_fixture_that_names_its_pair_keeps_it(self) -> None:
        pair = TerrainPair(closed="door-closed", open="water")
        gate = MapFeatureRecord(id="gate", kind="door", at=(3, 4), state="closed",
                                terrain=pair)
        ground = level(GROUND_LEVEL, "ground", GROUND_TILES, gate)
        assert gate.own_terrain(ground, LEGEND) == pair

    def test_a_door_that_names_none_is_what_a_door_has_always_been(self) -> None:
        ground = level(GROUND_LEVEL, "ground", GROUND_TILES, door())
        assert door().own_terrain(ground, LEGEND) == TerrainPair(
            closed="door-closed", open="door-open"
        )

    def test_anything_else_takes_its_own_tile_in_both_states(self) -> None:
        # A spike driven into a wall leaves a wall behind it whichever way it
        # is thrown. (0, 4) is wall on the ground plane.
        spike = MapFeatureRecord(id="spike", kind="spike", at=(0, 4), state="closed")
        ground = level(GROUND_LEVEL, "ground", GROUND_TILES, spike)
        assert spike.own_terrain(ground, LEGEND) == TerrainPair(closed="wall", open="wall")

    def test_an_upper_storeys_lever_takes_the_upper_storeys_tile(self) -> None:
        """The reason this takes the level and not the document.

        (2, 2) is ``difficult`` on the ground and plain ``floor`` in the
        gallery. A signature reaching for ``document.tiles`` reads the ground
        whatever storey it was asked about, so every upper-floor lever, spike
        and pressure plate would silently take the terrain of the room beneath
        it — and every existing case for this rule stands on the ground floor,
        where the two answers are the same.
        """
        lever = MapFeatureRecord(id="lever", kind="lever", at=(2, 2), state="closed")
        ground = level(GROUND_LEVEL, "ground", GROUND_TILES)
        upper = level(1, "gallery", UPPER_TILES, lever)
        assert lever.own_terrain(upper, LEGEND) == TerrainPair(closed="floor", open="floor")
        assert lever.own_terrain(ground, LEGEND) != lever.own_terrain(upper, LEGEND)


class TestClaims:
    """The yield order is the contract; both real callers build a dict from it."""

    def test_a_plain_door_claims_only_the_square_it_hangs_on(self) -> None:
        ground = level(GROUND_LEVEL, "ground", GROUND_TILES, door())
        claims = dict(door().claims(ground, LEGEND))
        assert list(claims) == [(3, 4)]
        assert claims[(3, 4)] == SquareClaim(
            feature="door-1",
            terrain=TerrainPair(closed="door-closed", open="door-open"),
            elevation=None,
        )

    def test_the_own_square_carries_the_resolved_pair_not_the_authored_one(self) -> None:
        # `terrain` is optional on the record, so a claims() that read it
        # straight would hand a door a claim with no terrain at all — which
        # falls through to the plane, and a door would stop being a door.
        assert door().terrain is None
        ground = level(GROUND_LEVEL, "ground", GROUND_TILES, door())
        claim = dict(door().claims(ground, LEGEND))[(3, 4)]
        assert claim.terrain == TerrainPair(closed="door-closed", open="door-open")

    def test_an_overlay_extends_the_claim_to_every_cell_it_names(self) -> None:
        gate = MapFeatureRecord(
            id="gate",
            kind="door",
            at=(3, 4),
            state="closed",
            terrain=TerrainPair(closed="wall", open="water"),
            affects=(
                MapOverlayRecord(
                    cells=((1, 3), (2, 3)),
                    terrain=TerrainPair(closed="floor", open="water"),
                ),
            ),
        )
        ground = level(GROUND_LEVEL, "ground", GROUND_TILES, gate)
        claims = dict(gate.claims(ground, LEGEND))
        assert sorted(claims) == [(1, 3), (2, 3), (3, 4)]
        assert {claim.feature for claim in claims.values()} == {"gate"}
        assert claims[(1, 3)].terrain == TerrainPair(closed="floor", open="water")
        assert claims[(3, 4)].terrain == TerrainPair(closed="wall", open="water")

    def test_an_overlay_may_move_height_without_touching_terrain(self) -> None:
        gate = MapFeatureRecord(
            id="gate",
            kind="gate",
            at=(1, 1),
            state="closed",
            affects=(
                MapOverlayRecord(cells=((3, 3),), elevation=HeightPair(closed=0, open=-5)),
            ),
        )
        ground = level(GROUND_LEVEL, "ground", GROUND_TILES, gate)
        claim = dict(gate.claims(ground, LEGEND))[(3, 3)]
        assert claim.terrain is None
        assert claim.elevation == HeightPair(closed=0, open=-5)

    def test_the_fixtures_own_square_may_carry_a_height_pair(self) -> None:
        gate = MapFeatureRecord(
            id="gate", kind="door", at=(3, 4), state="closed",
            elevation=HeightPair(closed=10, open=0),
        )
        ground = level(GROUND_LEVEL, "ground", GROUND_TILES, gate)
        assert dict(gate.claims(ground, LEGEND))[(3, 4)].elevation == HeightPair(
            closed=10, open=0
        )

    def test_a_fixture_claiming_one_square_twice_yields_it_twice(self) -> None:
        # claims() reports, it does not police: the duplicate is what the
        # document parser refuses, and it can only refuse what it can see.
        gate = MapFeatureRecord(
            id="gate",
            kind="gate",
            at=(1, 1),
            state="closed",
            affects=(
                MapOverlayRecord(
                    cells=((1, 1),), terrain=TerrainPair(closed="floor", open="water")
                ),
            ),
        )
        ground = level(GROUND_LEVEL, "ground", GROUND_TILES, gate)
        assert [square for square, _ in gate.claims(ground, LEGEND)] == [(1, 1), (1, 1)]
        # The own square comes first, and the assertion is on the *values* so a
        # reordering is caught: both real callers build a dict from this, where
        # the last claim for a square wins, so the yield order decides which of
        # two conflicting claims a caller keeps — and therefore which one its
        # refusal names. A stable order is what makes that diagnostic stable.
        both = [claim for _, claim in gate.claims(ground, LEGEND)]
        assert both[0].terrain == TerrainPair(closed="floor", open="floor")
        assert both[1].terrain == TerrainPair(closed="floor", open="water")

    def test_a_fixture_carries_its_prerequisites_and_its_price(self) -> None:
        # Held here because the runtime fixture type carried the same three and
        # had its own case for them; that type is gone and this record is what a
        # fight reads instead. A default that drifted would give every plain
        # door a prerequisite or a price.
        gate = MapFeatureRecord(
            id="sluice",
            kind="door",
            at=(8, 4),
            state="closed",
            requires=("north spike", "south spike"),
            costs_action=True,
            check=FeatureCheck(ability=Ability.STRENGTH, dc=15),
        )
        assert gate.requires == ("north spike", "south spike")
        assert gate.costs_action is True
        assert gate.check == FeatureCheck(ability=Ability.STRENGTH, dc=15)

    def test_a_plain_door_asks_nothing_and_costs_nothing(self) -> None:
        door = MapFeatureRecord(id="door", kind="door", at=(0, 0), state="closed")
        assert door.requires == ()
        assert door.costs_action is False
        assert door.check is None


class TestTheTypesStandAlone:
    def test_a_check_needs_only_the_kernels_ability(self) -> None:
        # The one thing in the tree that reaches outside grid geometry, and it
        # reaches into the kernel — which is the whole of what this module may
        # import. tests/test_layering.py holds the structural half of the claim.
        check = FeatureCheck(ability=Ability.STRENGTH, dc=15)
        record = MapFeatureRecord(
            id="spike", kind="spike", at=(0, 4), state="closed", check=check
        )
        ground = level(GROUND_LEVEL, "ground", GROUND_TILES, record)
        assert ground.fixtures()["spike"].check == check
