"""Pins for SRD 5.2.1's *Temporary Hit Points* on ``Creature``.

Four clauses, p.18:

* **Lose Temporary Hit Points First.** "If you have Temporary Hit Points and
  take damage, those points are lost first, and any leftover damage carries
  over to your Hit Points."
* **Duration.** "Temporary Hit Points last until they're depleted or you
  finish a Long Rest."
* **They Don't Stack.** "Temporary Hit Points can't be added together. If
  you have Temporary Hit Points and receive more of them, you decide whether
  to keep the ones you have or to gain the new ones."
* **They're Not Hit Points or Healing.** "Temporary Hit Points can't be
  added to your Hit Points, healing can't restore them, and receiving
  Temporary Hit Points doesn't count as healing. ... If you have 0 Hit
  Points, receiving Temporary Hit Points doesn't restore you to
  consciousness."

This engine has no player-choice channel at grant time, so the third clause
is implemented as "take the higher" — a deliberate simplification, pinned
below alongside the rest.
"""

from __future__ import annotations

from fivee_sim.kernel.conditions import Condition

from .conftest import fighter


def test_temp_hp_is_lost_before_hit_points() -> None:
    """The acceptance case: 5 temp HP, 7 damage, ends at max_hp - 2 with 0
    temp HP left — the SRD's own worked example, scaled onto a full-health
    fighter.
    """
    victim = fighter("Victim", max_hp=30)
    victim.temp_hp = 5
    victim.take_damage(7)
    assert victim.hp == victim.max_hp - 2
    assert victim.temp_hp == 0


def test_damage_within_the_buffer_never_touches_hit_points() -> None:
    victim = fighter("Victim", max_hp=30)
    victim.temp_hp = 10
    victim.take_damage(6)
    assert victim.hp == victim.max_hp
    assert victim.temp_hp == 4


def test_a_grant_to_an_unconscious_creature_does_not_wake_it() -> None:
    """"receiving Temporary Hit Points doesn't restore you to consciousness.
    Only true healing can save you." A creature at 0 hit points stays
    Unconscious after a grant.
    """
    victim = fighter("Victim", max_hp=30, hp=1)
    victim.take_damage(1)
    assert victim.hp == 0
    assert Condition.UNCONSCIOUS in victim.conditions
    victim.grant_temp_hp(5)
    assert victim.temp_hp == 5
    assert victim.hp == 0
    assert Condition.UNCONSCIOUS in victim.conditions


def test_a_grant_is_not_healing_and_leaves_death_saves_and_stable_alone() -> None:
    """The clause this is really about: receiving temp HP "doesn't count as
    healing." ``Creature.heal`` clears both death-save counters and
    ``stable`` and lifts Unconscious; a grant must not.
    """
    victim = fighter("Victim", max_hp=30, hp=1)
    victim.take_damage(1)
    victim.death_save_failures = 2
    victim.stable = True
    victim.grant_temp_hp(5)
    assert victim.death_save_failures == 2
    assert victim.stable
    assert Condition.UNCONSCIOUS in victim.conditions


def test_a_full_health_creature_can_still_receive_temp_hp() -> None:
    """"Because Temporary Hit Points aren't Hit Points, a creature can be at
    full Hit Points and receive Temporary Hit Points."
    """
    victim = fighter("Victim", max_hp=30)
    victim.grant_temp_hp(4)
    assert victim.hp == victim.max_hp
    assert victim.temp_hp == 4


def test_temp_hp_never_pushes_hit_points_past_the_maximum() -> None:
    victim = fighter("Victim", max_hp=30)
    victim.grant_temp_hp(100)
    assert victim.hp == victim.max_hp
    assert victim.temp_hp == 100


def test_a_grant_does_not_stack_and_keeps_the_higher_amount() -> None:
    """"Temporary Hit Points can't be added together" — this engine has no
    player-choice channel at grant time, so it takes the higher rather than
    asking, and neither a smaller nor a larger new grant may sum with what
    is already there.
    """
    victim = fighter("Victim", max_hp=30)
    victim.grant_temp_hp(5)
    victim.grant_temp_hp(3)
    assert victim.temp_hp == 5
    victim.grant_temp_hp(8)
    assert victim.temp_hp == 8


def test_a_negative_or_zero_grant_does_nothing() -> None:
    victim = fighter("Victim", max_hp=30)
    victim.temp_hp = 5
    victim.grant_temp_hp(0)
    victim.grant_temp_hp(-3)
    assert victim.temp_hp == 5


def test_a_dead_creature_cannot_be_granted_temp_hp() -> None:
    victim = fighter("Victim", max_hp=4, hp=4)
    victim.take_damage(20)
    assert victim.dead
    victim.grant_temp_hp(10)
    assert victim.temp_hp == 0


def test_temp_hp_absorbed_damage_does_not_count_toward_massive_damage_overflow() -> None:
    """SRD 5.2.1, "Damage at 0 Hit Points": "If the damage equals or exceeds
    your Hit Point maximum, you die." Instant death compares the damage
    *remaining after the drop* against the maximum — damage a temp HP buffer
    absorbed never reached hit points at all, so it must not count toward
    that remainder.

    Without the buffer, 20 damage into 4 max HP overflows by 16, well past
    the maximum of 4: instant death. With 15 temp HP soaking up 15 of the 20,
    only 5 reaches hit points, dropping the creature to 0 with an overflow of
    only 1 — short of the maximum, so it goes dying rather than dead.
    """
    victim = fighter("Victim", max_hp=4, hp=4)
    victim.temp_hp = 15
    victim.take_damage(20)
    assert not victim.dead
    assert victim.dying
    assert victim.hp == 0
    assert victim.temp_hp == 0
