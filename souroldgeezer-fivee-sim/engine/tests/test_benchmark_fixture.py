"""The level-1 benchmark suite, frozen as a regression fixture.

Fourteen fights pit the four benchmark pregens — Fighter, Cleric, Rogue, Wizard,
normalized to stat blocks from the battlecast.gg level-1 encounter write-ups —
against bundled and fixture-pack monsters in the mapless arena the benchmark
sessions used: party at x=0 (y 0/5/10/15), monsters at x=30 in 5 ft lanes,
seed 20260730, 20-round cap, one fresh ``Random(SEED + index)`` per iteration.
The source difficulty labels come from those write-ups too; they are recorded
here as prose provenance, not asserted — the engine is not obliged to agree
with them, only to keep agreeing with itself.

Band rubric, from the benchmark write-ups: win rate >= 0.90 reads easy/low,
>= 0.60 moderate, below that high/deadly.

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

SEED = 20260730
ITERATIONS = 400
MAX_ROUNDS = 20
WIN_TOLERANCE = 0.05
DOWN_TOLERANCE = 0.35

# name in the fixture pack, combatant label, spawn point
PARTY: tuple[tuple[str, str, Point], ...] = (
    ("Benchmark Fighter", "Fighter", (0, 5)),
    ("Benchmark Cleric", "Cleric", (0, 10)),
    ("Benchmark Rogue", "Rogue", (0, 0)),
    ("Benchmark Wizard", "Wizard", (0, 15)),
)
PC_NAMES = frozenset(label for _, label, _ in PARTY)
PARTY_SIZE = len(PARTY)

Roster = tuple[tuple[str, int], ...]


class Fight(NamedTuple):
    title: str
    roster: Roster
    source_label: str | None  # difficulty per the battlecast.gg write-up; None = folklore
    expected_win: float  # calibrated on this engine at ITERATIONS iterations
    expected_down: float  # calibrated expected PCs down at the end of the fight


FIGHTS: tuple[Fight, ...] = (
    Fight("2 Goblin Warriors", (("Goblin Warrior", 2),), "easy/low", 1.0000, 0.2500),
    Fight("Zombie + Skeleton", (("Zombie", 1), ("Skeleton", 1)), "easy/low", 1.0000, 0.2625),
    Fight("4 Goblin Warriors", (("Goblin Warrior", 4),), "low", 0.9300, 1.0825),
    Fight("4 Skeletons", (("Skeleton", 4),), "low", 0.7775, 1.7425),
    Fight("4 Wolves", (("Wolf", 4),), "low", 0.8700, 1.2225),
    Fight("6 Goblin Warriors", (("Goblin Warrior", 6),), "moderate", 0.4975, 2.6150),
    Fight(
        "Goblin Boss + 2 Warriors",
        (("Goblin Boss", 1), ("Goblin Warrior", 2)),
        "moderate", 0.9025, 1.2275,
    ),
    Fight(
        "3 Goblins + 3 Skeletons",
        (("Goblin Warrior", 3), ("Skeleton", 3)),
        "moderate", 0.2650, 3.3300,
    ),
    Fight("Ogre + Goblin Boss", (("Ogre", 1), ("Goblin Boss", 1)), "deadly", 0.5325, 2.6175),
    Fight("1 Ogre (folklore)", (("Ogre", 1),), None, 0.9350, 0.9825),
    Fight("3 Giant Wasps", (("Giant Wasp", 3),), "moderate", 0.4675, 2.7525),
    Fight("4 Giant Venomous Snakes", (("Giant Venomous Snake", 4),), "low", 0.3625, 3.1000),
    Fight("3 Giant Venomous Snakes", (("Giant Venomous Snake", 3),), "low", 0.7175, 2.0575),
    Fight(
        "2 Snakes + 3 Goblins",
        (("Giant Venomous Snake", 2), ("Goblin Warrior", 3)),
        "low", 0.5400, 2.6550,
    ),
)


def band(win: float) -> str:
    """The benchmark write-ups' difficulty rubric, applied to a simulated win rate."""
    return "easy/low" if win >= 0.90 else "moderate" if win >= 0.60 else "high/deadly"


def band_is_stable(expected_win: float) -> bool:
    """True when no win rate passing the ±WIN_TOLERANCE window can change band."""
    return all(abs(expected_win - edge) >= WIN_TOLERANCE for edge in (0.90, 0.60))


#: The fights that go without a band assertion, named rather than left to a
#: branch nobody sees. Their calibrated win rate sits close enough to an edge
#: that a rate still inside its own ±WIN_TOLERANCE window could cross it, so the
#: assertion would flap. Pinning the set is what makes the omission reviewable:
#: a recalibration that moves a fight on or off this list has to say so in the
#: same commit, rather than quietly dropping a third of the fights' band checks.
UNBANDED = (
    "4 Goblin Warriors",
    "4 Wolves",
    "Goblin Boss + 2 Warriors",
    "1 Ogre (folklore)",
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
    # Zombie, Ogre, and Guiding Bolt; the fixture pack supplies only what it
    # lacks (the pregens, the two remaining monsters, the wizard's spells).
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
        f"(source label: {fight.source_label or 'none'}); if the engine change was "
        f"intentional, recalibrate the constants in this module"
    )
    assert down == pytest.approx(fight.expected_down, abs=DOWN_TOLERANCE), (
        f"expected PCs down {down:.4f} drifted from calibrated {fight.expected_down:.4f}; "
        f"if the engine change was intentional, recalibrate the constants in this module"
    )
    if band_is_stable(fight.expected_win):
        assert band(win) == band(fight.expected_win)
