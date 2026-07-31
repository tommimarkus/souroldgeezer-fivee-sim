"""What a fixture claims, and what each claimed square is in either state.

:meth:`MapFeature.claims` is the single derivation of "which squares does this
fixture govern, and what is each one in either state". Two callers need it —
``Encounter._adopt_map`` builds the live index from it, and
``service.maps.query`` builds the stateless mirror it promises to keep matching
— so the knowledge lives here rather than being derived twice and drifting.

The tests below are about that derivation only. What a claim *means* to a fight
belongs to ``test_encounter.py``; how it is spelled in a file belongs to
``test_map_document.py``.
"""

from __future__ import annotations

from fivee_sim.kernel.rules import Ability
from fivee_sim.model.battlemap import (
    FeatureCheck,
    FeatureOverlay,
    HeightPair,
    MapFeature,
    TerrainPair,
)


class TestClaims:
    def test_a_plain_door_claims_only_the_square_it_hangs_on(self) -> None:
        door = MapFeature(name="door-east", square=(5, 2))
        claims = dict(door.claims())
        assert list(claims) == [(5, 2)]
        assert claims[(5, 2)].feature == "door-east"
        assert claims[(5, 2)].terrain == TerrainPair(closed="door-closed", open="door-open")

    def test_an_overlay_extends_the_claim_to_every_cell_it_names(self) -> None:
        sluice = MapFeature(
            name="sluice",
            square=(8, 4),
            closed_terrain="wall",
            open_terrain="water",
            affects=(
                FeatureOverlay(
                    squares=((1, 5), (2, 5)),
                    terrain=TerrainPair(closed="floor", open="water"),
                ),
            ),
        )
        claims = dict(sluice.claims())
        assert sorted(claims) == [(1, 5), (2, 5), (8, 4)]
        # Every claimed square names the fixture that governs it, which is what
        # lets one index answer "who owns this square" and "what is it now".
        assert {claim.feature for claim in claims.values()} == {"sluice"}
        assert claims[(1, 5)].terrain == TerrainPair(closed="floor", open="water")
        assert claims[(8, 4)].terrain == TerrainPair(closed="wall", open="water")

    def test_an_overlay_may_move_height_without_touching_terrain(self) -> None:
        # The water level rising is a height change over squares whose terrain
        # the same fixture may or may not also change; a claim with no terrain
        # pair falls through to the plane, which is what keeps the two layers
        # independent.
        gate = MapFeature(
            name="gate",
            square=(0, 0),
            affects=(
                FeatureOverlay(
                    squares=((3, 3),), elevation=HeightPair(closed=0, open=-5)
                ),
            ),
        )
        claim = dict(gate.claims())[(3, 3)]
        assert claim.terrain is None
        assert claim.elevation == HeightPair(closed=0, open=-5)

    def test_the_fixtures_own_square_may_carry_a_height_pair(self) -> None:
        gate = MapFeature(name="gate", square=(2, 2), elevation=HeightPair(closed=10, open=0))
        assert dict(gate.claims())[(2, 2)].elevation == HeightPair(closed=10, open=0)

    def test_a_fixture_claiming_one_square_twice_yields_it_twice(self) -> None:
        # claims() reports, it does not police: the duplicate is what the
        # document parser and _adopt_map each refuse, and they can only refuse
        # what they can see.
        gate = MapFeature(
            name="gate",
            square=(1, 1),
            closed_terrain="wall",
            open_terrain="wall",
            affects=(
                FeatureOverlay(
                    squares=((1, 1),), terrain=TerrainPair(closed="floor", open="water")
                ),
            ),
        )
        assert [square for square, _ in gate.claims()] == [(1, 1), (1, 1)]
        # The own square comes first, and the assertion is on the *values* so a
        # reordering is caught: both real callers build a dict from this, where
        # the last claim for a square wins, so the yield order decides which of
        # two conflicting claims a caller keeps — and therefore which one its
        # refusal names. A stable order is what makes that diagnostic stable.
        both = [claim for _, claim in gate.claims()]
        assert both[0].terrain == TerrainPair(closed="wall", open="wall")
        assert both[1].terrain == TerrainPair(closed="floor", open="water")


class TestGates:
    def test_a_fixture_carries_its_prerequisites_and_its_price(self) -> None:
        gate = MapFeature(
            name="sluice",
            square=(8, 4),
            requires=("north spike", "south spike"),
            costs_action=True,
            check=FeatureCheck(ability=Ability.STRENGTH, dc=15),
        )
        assert gate.requires == ("north spike", "south spike")
        assert gate.costs_action is True
        assert gate.check == FeatureCheck(ability=Ability.STRENGTH, dc=15)

    def test_a_plain_door_asks_nothing_and_costs_nothing(self) -> None:
        door = MapFeature(name="door", square=(0, 0))
        assert door.requires == ()
        assert door.costs_action is False
        assert door.check is None
