"""Pins for SRD 5.2.1 healing clauses that already work but were barely tested.

Before this file, the whole suite had exactly one call to ``Creature.heal``
(``tests/test_encounter.py::TestGoingDown::test_healing_from_zero_clears_unconsciousness_and_resets_saves``).
A later refactor touches all three ``heal`` call sites, so these characterisation
tests exist to catch a regression there — each one is already green on the
current implementation, which is expected for a characterisation test.
"""

from __future__ import annotations

from random import Random

from fivee_sim.kernel.conditions import Condition
from fivee_sim.model.creature import Creature
from fivee_sim.model.encounter import Action, ActionKind, Encounter

from .conftest import FixedRandom, advance_to, fighter


def test_healing_in_excess_of_the_maximum_is_lost() -> None:
    """SRD 5.2.1, "Hit Points", Hit Point Maximum: "any Hit Points regained in
    excess of the maximum are lost."
    """
    victim = fighter("Victim", max_hp=20, hp=14)
    victim.heal(8)
    assert victim.hp == 20


def test_a_dead_creature_cannot_be_healed() -> None:
    """SRD 5.2.1 glossary, "Dead": a dead creature "can't regain [hit points]
    unless it is first revived by magic".

    Killed here by "Damage at 0 Hit Points" — "If the damage equals or exceeds
    your Hit Point maximum, you die" — and deliberately not by Instant Death's
    Massive Damage clause, which is about a *drop* to 0 leaving a remainder. Both
    end in ``dead``, and this is the branch that reaches it with hp already at 0
    before ``heal`` is ever called, so the refusal cannot pass by way of the cap.
    """
    victim = fighter("Victim", max_hp=25, hp=1)
    victim.take_damage(1)
    victim.take_damage(30)
    assert victim.dead
    assert victim.hp == 0
    victim.heal(10)
    assert victim.dead
    assert victim.hp == 0


def test_healing_from_zero_does_not_clear_prone() -> None:
    """Nothing in the SRD ends Prone on healing — "Falling Unconscious" (SRD
    5.2.1) ends only the Unconscious condition when hit points are regained, and
    a creature that stands up must still spend the movement "Standing Up" costs.
    """
    victim = fighter("Victim", max_hp=20, hp=1)
    victim.take_damage(1)
    assert Condition.PRONE in victim.conditions
    victim.heal(5)
    assert Condition.PRONE in victim.conditions


def test_healing_clears_unconsciousness_and_resets_both_death_save_counters() -> None:
    """SRD 5.2.1, "Death Saving Throws", Three Successes/Failures: "The number of
    both is reset to zero when you regain any Hit Points or become Stable."

    The one existing ``heal`` test only set failures beforehand; this one also
    sets successes, so a fix that clears failures alone would still fail here.
    """
    victim = fighter("Victim", max_hp=20, hp=1)
    victim.take_damage(1)
    victim.death_save_successes = 2
    victim.death_save_failures = 1
    victim.heal(5)
    assert Condition.UNCONSCIOUS not in victim.conditions
    assert victim.death_save_successes == 0
    assert victim.death_save_failures == 0


def test_healing_a_stable_creature_wakes_it_and_clears_stable() -> None:
    """SRD 5.2.1, "Stabilizing a Character": a Stable creature still has the
    Unconscious condition and regains hit points only through ordinary healing
    or the passage of time — either way, regaining hit points wakes it.
    """
    victim = fighter("Victim", max_hp=20, hp=1)
    victim.take_damage(1)
    victim.stable = True
    assert Condition.UNCONSCIOUS in victim.conditions
    victim.heal(5)
    assert not victim.stable
    assert Condition.UNCONSCIOUS not in victim.conditions
    assert victim.hp == 5


def test_damage_while_stable_ends_stability() -> None:
    """SRD 5.2.1, "Stabilizing a Character": "If the creature takes damage, it
    stops being Stable and starts making Death Saving Throws again."
    """
    victim = fighter("Victim", max_hp=20, hp=1)
    victim.take_damage(1)
    victim.stable = True
    victim.take_damage(3)
    assert not victim.stable
    assert victim.dying


class TestNaturalTwentyRoutesThroughHeal:
    """SRD 5.2.1, "Death Saving Throws", Rolling a 1 or 20: "If you roll a 20 on
    the d20, you regain 1 Hit Point."

    ``Encounter._death_save`` calls ``creature.heal(1)`` on a natural 20 rather
    than waking the creature and resetting its counters itself — this is the
    test that would fail if someone reimplemented that wake logic inline in
    ``_death_save`` instead of delegating to ``Creature.heal``, since ``heal``
    is what both clears Unconscious and resets both death-save counters.
    """

    @staticmethod
    def _dying_hero() -> tuple[Encounter, Creature]:
        rng = Random(8)
        hero = fighter("Hero", max_hp=30, hp=1, position=0)
        foe = fighter("Foe", team="foes", position=5)
        ally = fighter("Ally", position=40)  # keeps the fight from ending
        encounter = Encounter([hero, foe, ally], rng)
        advance_to(encounter, "Foe", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Hero"), FixedRandom(20))
        assert hero.dying
        return encounter, hero

    def test_natural_20_wakes_the_creature_and_resets_both_counters(self) -> None:
        encounter, hero = self._dying_hero()
        hero.death_save_successes = 1
        hero.death_save_failures = 2
        # advance_to feeds this forced roll to whichever begin_turn it advances
        # into, so it lands on Hero's own death save, not the attacker's action.
        advance_to(encounter, "Hero", FixedRandom(20))
        assert hero.conscious
        assert hero.hp == 1
        assert hero.death_save_successes == 0
        assert hero.death_save_failures == 0
