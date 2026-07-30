"""Exact expected-damage arithmetic for the auto-play policy.

Nothing here rolls a die or touches state. It answers the single question the
policy needs — "which option is worth more this turn?" — and answers it *exactly*,
by enumerating the damage distribution rather than approximating with a mean.

Two things make that worth the effort:

* **Rounding is preserved.** A damage total is clamped at zero, a made save halves
  the *rolled total* and rounds down, and Resistance halves and rounds down again.
  ``E[floor(x / 2)]`` is not ``E[x] / 2``, so the arithmetic runs over the whole
  distribution and every outcome is passed through the kernel's own
  ``effective_damage`` rather than a local copy of that rule.
* **Nothing is re-derived.** Advantage and critical-hit rules are not
  reimplemented here; callers hand in the state the kernel's own
  ``compute_attack_advantage`` and ``melee_hit_is_critical`` computed. A policy
  that re-derived them would drift from the rules it is meant to be choosing under,
  which is the same failure this module exists to prevent.

``tests/test_expectation.py`` pins every function below against the kernel actually
rolling. That test is the reason these numbers can be trusted.
"""

from __future__ import annotations

from functools import cache

from ..kernel.dice import Advantage, Dice
from ..kernel.rules import effective_damage


@cache
def _sum_distribution(count: int, faces: int) -> tuple[float, ...]:
    """``P(total == index)`` for ``count`` dice of ``faces``, indexed 0..count*faces."""
    distribution = [0.0] * (count * faces + 1)
    distribution[0] = 1.0
    for rolled in range(count):
        updated = [0.0] * (count * faces + 1)
        highest = rolled * faces
        for total in range(highest + 1):
            probability = distribution[total]
            if probability == 0.0:
                continue
            share = probability / faces
            for face in range(1, faces + 1):
                updated[total + face] += share
        distribution = updated
    return tuple(distribution)


@cache
def _natural_distribution(advantage: Advantage) -> tuple[float, ...]:
    """``P(natural == index)`` for a d20 under ``advantage``, indexed 0..20."""
    probabilities = [0.0] * 21
    for natural in range(1, 21):
        if advantage is Advantage.ADVANTAGE:
            probabilities[natural] = (2 * natural - 1) / 400
        elif advantage is Advantage.DISADVANTAGE:
            probabilities[natural] = (2 * (21 - natural) - 1) / 400
        else:
            probabilities[natural] = 1 / 20
    return tuple(probabilities)


@cache
def expected_damage(
    dice: Dice,
    *,
    critical: bool = False,
    halved: bool = False,
    resisted: bool = False,
    vulnerable: bool = False,
    immune: bool = False,
) -> float:
    """Expected damage from one roll of ``dice``, with the kernel's own rounding.

    ``halved`` is the made-save case: the rolled total is halved and rounded down
    *before* Resistance applies, matching ``resolve_spell``.
    """
    if immune:
        return 0.0
    count = dice.count * 2 if critical else dice.count
    expected = 0.0
    for rolled, probability in enumerate(_sum_distribution(count, dice.faces)):
        if probability == 0.0:
            continue
        # DiceRoll.total clamps at zero before anything else touches it.
        total = max(0, rolled + dice.modifier)
        if halved:
            total //= 2
        expected += probability * effective_damage(
            total, resisted=resisted, vulnerable=vulnerable, immune=immune
        )
    return expected


@cache
def _rolled_distribution(dice: Dice, *, critical: bool = False) -> tuple[float, ...]:
    """``P(DiceRoll.total == index)`` for one roll of ``dice``.

    ``DiceRoll.total`` clamps at zero per roll, so the clamp is applied here —
    before any sum with another roll — exactly as the kernel does it.
    """
    count = dice.count * 2 if critical else dice.count
    top = max(0, count * dice.faces + dice.modifier)
    totals = [0.0] * (top + 1)
    for rolled, probability in enumerate(_sum_distribution(count, dice.faces)):
        totals[max(0, rolled + dice.modifier)] += probability
    return tuple(totals)


@cache
def _hit_damage(
    damage: Dice,
    extra: Dice | None,
    *,
    critical: bool = False,
    resisted: bool = False,
    vulnerable: bool = False,
    immune: bool = False,
) -> float:
    """Expected damage of one landed hit's same-type pool.

    ``extra`` is the Advantage rider's dice: it shares the main damage type, so
    the kernel sums the two rolls **before** ``effective_damage`` halves or
    doubles — one damage instance, one rounding. That is why this enumerates the
    joint distribution rather than adding two separately-halved expectations,
    which would drift by the floor on odd totals.
    """
    if extra is None:
        return expected_damage(
            damage, critical=critical, resisted=resisted, vulnerable=vulnerable,
            immune=immune,
        )
    if immune:
        return 0.0
    expected = 0.0
    for main_total, main_probability in enumerate(
        _rolled_distribution(damage, critical=critical)
    ):
        if main_probability == 0.0:
            continue
        for extra_total, extra_probability in enumerate(
            _rolled_distribution(extra, critical=critical)
        ):
            if extra_probability == 0.0:
                continue
            expected += main_probability * extra_probability * effective_damage(
                main_total + extra_total,
                resisted=resisted, vulnerable=vulnerable, immune=immune,
            )
    return expected


def attack_damage_expectation(
    *,
    attack_bonus: int,
    target_ac: int,
    damage: Dice,
    advantage: Advantage = Advantage.NONE,
    forced_critical: bool = False,
    resisted: bool = False,
    vulnerable: bool = False,
    immune: bool = False,
    advantage_bonus_damage: Dice | None = None,
    bonus_damage: Dice | None = None,
    bonus_resisted: bool = False,
    bonus_vulnerable: bool = False,
    bonus_immune: bool = False,
) -> float:
    """Expected damage from one attack, mirroring ``AttackRoll``'s hit and crit rules.

    The riders are valued exactly as ``resolve_attack`` rolls them.
    ``advantage_bonus_damage`` counts only when ``advantage`` — the resolved
    state the caller read off the encounter, never re-derived here — is
    Advantage, and it joins the main pool before that pool's defenses round.
    ``bonus_damage`` is the secondary pool, defended by the ``bonus_*`` flags
    against its own type. Every rider's dice double on a critical hit exactly as
    the main dice do.
    """
    extra = advantage_bonus_damage if advantage is Advantage.ADVANTAGE else None
    normal = _hit_damage(
        damage, extra, resisted=resisted, vulnerable=vulnerable, immune=immune
    )
    critical = _hit_damage(
        damage, extra, critical=True, resisted=resisted, vulnerable=vulnerable,
        immune=immune,
    )
    if bonus_damage is not None:
        normal += expected_damage(
            bonus_damage, resisted=bonus_resisted, vulnerable=bonus_vulnerable,
            immune=bonus_immune,
        )
        critical += expected_damage(
            bonus_damage, critical=True, resisted=bonus_resisted,
            vulnerable=bonus_vulnerable, immune=bonus_immune,
        )
    expected = 0.0
    for natural, probability in enumerate(_natural_distribution(advantage)):
        if probability == 0.0:
            continue
        if natural == 20:
            expected += probability * critical  # a natural 20 always hits and crits
        elif natural == 1:
            continue  # a natural 1 always misses whatever the bonus
        elif natural + attack_bonus >= target_ac:
            expected += probability * (critical if forced_critical else normal)
    return expected


def save_damage_expectation(
    *,
    save_dc: int,
    save_modifier: int,
    damage: Dice,
    half_on_save: bool = True,
    has_save: bool = True,
    auto_fail: bool = False,
    resisted: bool = False,
    vulnerable: bool = False,
    immune: bool = False,
) -> float:
    """Expected damage a save-based spell deals one target.

    ``has_save`` false is a spell that simply lands — ``resolve_spell`` treats the
    absence of a saving throw as a failed one rather than a successful one.
    """
    full = expected_damage(
        damage, resisted=resisted, vulnerable=vulnerable, immune=immune
    )
    if auto_fail or not has_save:
        return full
    made = sum(
        probability
        for natural, probability in enumerate(_natural_distribution(Advantage.NONE))
        if natural and natural + save_modifier >= save_dc
    )
    if not half_on_save:
        return (1.0 - made) * full
    halved = expected_damage(
        damage, halved=True, resisted=resisted, vulnerable=vulnerable, immune=immune
    )
    return (1.0 - made) * full + made * halved
