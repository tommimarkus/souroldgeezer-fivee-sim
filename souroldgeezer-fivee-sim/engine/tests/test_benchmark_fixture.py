"""The level-1 benchmark suite, frozen as a regression fixture.

Fourteen fights pit four project-authored abstract level-1 profiles — Vanguard,
Warden, Skirmisher, and Arcanist — against a project-authored progression of
bundled and fixture-pack monsters. SRD 5.2.1 supplies the game statistics under
the repository's NOTICE; the profile choices, encounter matrix, rubric, and
calibration are original to this project. The mapless arena puts the party at
x=0 (y 0/5/10/15) and monsters at x=30 in 5 ft lanes, with seed 20260807, a
20-round cap, and one fresh ``Random(SEED + index)`` per iteration.

Project rubric: win rate >= 0.85 reads comfortable, >= 0.50 contested, and
anything below that severe.

**What a failure means.** The constants below were calibrated on this engine at
ITERATIONS iterations. The run is deterministic under the fixed seed sequence,
so on an unchanged engine the measured values reproduce exactly; the windows
exist for refactors that reorder RNG consumption, which redraws the sample.
At n=400 the standard error of a win rate near 0.5 is ~0.025, so the ±0.05
window is ±2σ — an innocuous reordering rarely trips it, while a real rules
change of a few points lands outside. Expected-PCs-down gets ±0.35: under half
a downed character, far above the ~0.06 standard error. A fight's band is
asserted only where it cannot flap: when the calibrated win rate sits within
WIN_TOLERANCE of a band edge, a win rate that still passes its own window could
cross the edge, so the band assertion is skipped for that fight. Which fights
those are is not left to this prose — ``UNBANDED`` names them and a test pins
it, so a recalibration cannot move one on or off the list in silence.

**If an intentional engine change moves a number: recalibrate the constants,
do not widen the windows.** Rerun the loop below at ITERATIONS on the new
engine, paste in the fresh values, and say in the commit why the behaviour
moved. Loosening a tolerance converts the fixture from a tripwire into a
formality. Note the calibration on this branch already includes the stand act
(prone creatures get up at turn start for half Speed), so wolf figures differ
from any earlier benchmark session's.

This calibration also includes ranged attacks in close combat: a ranged weapon
or ranged spell attack has Disadvantage while a capable, seeing enemy is within
5 feet of the attacker.

This calibration also treats ordinary content-pack monsters as dead at 0 HP;
only combatants that explicitly author ``death_rule: death_saves`` keep taking
death-save turns. That removes the old PC lifecycle from monster initiative and
changes later RNG consumption even in fights whose final rate moves only a few
points.
"""

from __future__ import annotations

from pathlib import Path
from random import Random
from statistics import fmean
from typing import NamedTuple

import pytest

from fivee_sim.analytics.montecarlo import run_encounter
from fivee_sim.content import ContentRegistry, load_packs, make_creature
from fivee_sim.kernel.grid import Point
from fivee_sim.model.creature import Creature
from fivee_sim.model.encounter import Encounter

FIXTURE = Path(__file__).parent / "benchmark-fixture-pack.json"

SEED = 20260807
ITERATIONS = 400
MAX_ROUNDS = 20
WIN_TOLERANCE = 0.05
DOWN_TOLERANCE = 0.35

# name in the fixture pack, combatant label, spawn point
PARTY: tuple[tuple[str, str, Point], ...] = (
    ("Benchmark Vanguard", "Vanguard", (0, 5)),
    ("Benchmark Warden", "Warden", (0, 10)),
    ("Benchmark Skirmisher", "Skirmisher", (0, 0)),
    ("Benchmark Arcanist", "Arcanist", (0, 15)),
)
PC_NAMES = frozenset(label for _, label, _ in PARTY)
PARTY_SIZE = len(PARTY)

Roster = tuple[tuple[str, int], ...]


class Fight(NamedTuple):
    title: str
    roster: Roster
    expected_win: float  # calibrated on this engine at ITERATIONS iterations
    expected_down: float  # calibrated expected PCs down at the end of the fight


FIGHTS: tuple[Fight, ...] = (
    Fight("1 Goblin Warrior", (("Goblin Warrior", 1),), 1.0000, 0.0800),
    Fight("3 Goblin Warriors", (("Goblin Warrior", 3),), 0.9825, 0.4550),
    Fight("5 Goblin Warriors", (("Goblin Warrior", 5),), 0.6400, 2.0725),
    Fight("2 Skeletons", (("Skeleton", 2),), 0.9975, 0.4850),
    Fight("3 Skeletons", (("Skeleton", 3),), 0.9225, 0.9925),
    Fight("2 Wolves", (("Wolf", 2),), 1.0000, 0.1525),
    Fight("3 Wolves", (("Wolf", 3),), 0.9700, 0.5125),
    Fight(
        "Goblin Boss + Warrior",
        (("Goblin Boss", 1), ("Goblin Warrior", 1)),
        0.9725,
        0.5875,
    ),
    Fight("1 Ogre", (("Ogre", 1),), 0.9275, 1.0475),
    Fight("Ogre + Skeleton", (("Ogre", 1), ("Skeleton", 1)), 0.6575, 2.0825),
    Fight("1 Giant Wasp", (("Giant Wasp", 1),), 1.0000, 0.2950),
    Fight("2 Giant Wasps", (("Giant Wasp", 2),), 0.8900, 1.2175),
    Fight("1 Giant Venomous Snake", (("Giant Venomous Snake", 1),), 1.0000, 0.2900),
    Fight("5 Giant Venomous Snakes", (("Giant Venomous Snake", 5),), 0.0600, 3.8550),
)


def band(win: float) -> str:
    """Apply this project's coarse outcome rubric to a simulated win rate."""
    return "comfortable" if win >= 0.85 else "contested" if win >= 0.50 else "severe"


def band_is_stable(expected_win: float) -> bool:
    """True when no win rate passing the ±WIN_TOLERANCE window can change band."""
    return all(abs(expected_win - edge) >= WIN_TOLERANCE for edge in (0.85, 0.50))


#: The fights that go without a band assertion, named rather than left to a
#: branch nobody sees. Their calibrated win rate sits close enough to an edge
#: that a rate still inside its own ±WIN_TOLERANCE window could cross it, so the
#: assertion would flap. Pinning the set is what makes the omission reviewable:
#: a recalibration that moves a fight on or off this list has to say so in the
#: same commit, rather than quietly dropping a third of the fights' band checks.
UNBANDED = (
    "2 Giant Wasps",
)


def test_the_fights_that_skip_the_band_assertion_are_the_ones_named() -> None:
    skipped = tuple(
        fight.title for fight in FIGHTS if not band_is_stable(fight.expected_win)
    )
    assert skipped == UNBANDED, (
        "the set of fights going without a band assertion has moved; a "
        "recalibration has added or dropped one, and UNBANDED must say which"
    )


@pytest.fixture(scope="module")
def registry() -> ContentRegistry:
    # The bundled slice supplies Goblin Warrior, Goblin Boss, Skeleton, Wolf,
    # Zombie, Ogre, Guiding Bolt, and Fire Bolt; the fixture pack supplies only
    # what it lacks (the pregens, the two remaining monsters, Magic Missile).
    return load_packs([FIXTURE], include_environment=False)


def build(roster: Roster, registry: ContentRegistry) -> list[Creature]:
    creatures = [
        make_creature(name, registry=registry, label=label, position=position)
        for name, label, position in PARTY
    ]
    slot = 0
    for name, count in roster:
        for index in range(count):
            creatures.append(
                make_creature(
                    name, registry=registry,
                    label=f"{name} {index + 1}" if count > 1 else name,
                    position=(30, slot * 5),
                )
            )
            slot += 1
    return creatures


@pytest.mark.parametrize("fight", FIGHTS, ids=[fight.title for fight in FIGHTS])
def test_fight_stays_calibrated(fight: Fight, registry: ContentRegistry) -> None:
    wins = 0
    downs: list[int] = []
    for index in range(ITERATIONS):
        rng = Random(SEED + index)
        encounter = Encounter(
            build(fight.roster, registry), rng,
            spellbook=registry.spells, condition_effects=registry.condition_effects,
        )
        outcome = run_encounter(encounter, rng, max_rounds=MAX_ROUNDS)
        wins += outcome.winner == "party"
        downs.append(PARTY_SIZE - sum(1 for name in outcome.survivors if name in PC_NAMES))
    win = wins / ITERATIONS
    down = fmean(downs)

    assert win == pytest.approx(fight.expected_win, abs=WIN_TOLERANCE), (
        f"win rate {win:.4f} drifted from calibrated {fight.expected_win:.4f} "
        "if the engine change was intentional, recalibrate the constants in this module"
    )
    assert down == pytest.approx(fight.expected_down, abs=DOWN_TOLERANCE), (
        f"expected PCs down {down:.4f} drifted from calibrated {fight.expected_down:.4f}; "
        f"if the engine change was intentional, recalibrate the constants in this module"
    )
    if band_is_stable(fight.expected_win):
        assert band(win) == band(fight.expected_win)
