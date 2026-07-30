"""Spell resolution tests.

The two orderings that matter: an area spell rolls its damage once for everyone
in it, and an attack-roll spell resolves the attack before its damage so a
critical can double the dice.
"""

from __future__ import annotations

from random import Random

import pytest

from fivee_sim.data import spellbook
from fivee_sim.kernel.conditions import Condition
from fivee_sim.kernel.dice import Dice
from fivee_sim.kernel.rules import Ability, DamageType
from fivee_sim.kernel.spells import Spell, SpellTarget, resolve_spell

from .test_kernel import FixedRandom


def _fireball() -> Spell:
    return spellbook()["Fireball"]


class TestAreaSpells:
    def test_damage_is_rolled_once_and_shared_by_every_target(self) -> None:
        # One target cannot save, the other cannot fail. If damage were rolled per
        # target, the failing target's damage would not be exactly twice the
        # saving target's halved share of a single roll.
        resolution = resolve_spell(
            Random(11),
            _fireball(),
            slot_level=3,
            save_dc=15,
            targets=(
                SpellTarget(name="Fails", save_modifier=-20),
                SpellTarget(name="Saves", save_modifier=+40),
            ),
        )
        assert resolution.damage_roll is not None
        base = resolution.damage_roll.total
        failed, saved = resolution.results
        assert not failed.affected or failed.damage_dealt == base
        assert saved.damage_dealt == base // 2

    def test_a_target_that_saves_takes_nothing_when_the_spell_has_no_half_effect(self) -> None:
        spell = Spell(
            name="All or Nothing",
            level=1,
            save_ability=Ability.DEXTERITY,
            damage=Dice(2, 6),
            damage_type=DamageType.FIRE,
            half_on_save=False,
            provenance="synthetic test fixture, not SRD content",
        )
        resolution = resolve_spell(
            Random(3),
            spell,
            slot_level=1,
            save_dc=10,
            targets=(SpellTarget(name="Saves", save_modifier=+40),),
        )
        assert resolution.results[0].damage_dealt == 0

    def test_auto_failed_save_takes_full_damage(self) -> None:
        resolution = resolve_spell(
            Random(5),
            _fireball(),
            slot_level=3,
            save_dc=10,
            targets=(SpellTarget(name="Paralyzed", save_modifier=+40, auto_fail_save=True),),
        )
        assert resolution.damage_roll is not None
        assert resolution.results[0].damage_dealt == resolution.damage_roll.total

    def test_resistance_applies_after_the_save(self) -> None:
        resolution = resolve_spell(
            Random(9),
            _fireball(),
            slot_level=3,
            save_dc=10,
            targets=(
                SpellTarget(name="Plain", save_modifier=-20),
                SpellTarget(name="Resistant", save_modifier=-20, resisted=True),
            ),
        )
        plain, resistant = resolution.results
        assert resistant.damage_dealt == plain.damage_dealt // 2


class TestUpcasting:
    def test_fireball_gains_a_die_per_slot_level(self) -> None:
        spell = _fireball()
        assert str(spell.damage_at(3)) == "8d6"
        assert str(spell.damage_at(5)) == "10d6"
        assert str(spell.damage_at(9)) == "14d6"

    def test_casting_below_the_spells_level_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be cast"):
            resolve_spell(
                Random(1),
                _fireball(),
                slot_level=2,
                save_dc=15,
                targets=(SpellTarget(name="Anyone"),),
            )

    def test_a_spell_without_upcast_dice_does_not_scale(self) -> None:
        hold = spellbook()["Hold Person"]
        assert hold.damage_at(5) is None


class TestAttackRollSpells:
    def test_critical_doubles_the_spells_dice(self) -> None:
        bolt = spellbook()["Guiding Bolt"]

        def cast(natural: int) -> int:
            resolution = resolve_spell(
                FixedRandom(natural),
                bolt,
                slot_level=1,
                save_dc=15,
                spell_attack_bonus=5,
                targets=(SpellTarget(name="Struck", ac=10),),
            )
            result = resolution.results[0]
            assert result.attack is not None
            assert result.attack.hit
            return result.damage_dealt

        # Both rolls land; only the natural 20 crits. Guiding Bolt is 4d6, so a
        # maximised normal hit is 24 and a maximised critical is 8d6 = 48.
        assert cast(19) == 4 * 6
        assert cast(20) == 8 * 6

    def test_a_miss_deals_nothing(self) -> None:
        bolt = spellbook()["Guiding Bolt"]
        resolution = resolve_spell(
            FixedRandom(1),
            bolt,
            slot_level=1,
            save_dc=15,
            spell_attack_bonus=5,
            targets=(SpellTarget(name="Untouched", ac=10),),
        )
        assert resolution.results[0].damage_dealt == 0
        assert not resolution.results[0].affected


class TestConditionSpells:
    def test_hold_person_paralyzes_on_a_failed_save_only(self) -> None:
        hold = spellbook()["Hold Person"]
        failed = resolve_spell(
            Random(2), hold, slot_level=2, save_dc=15,
            targets=(SpellTarget(name="Held", save_modifier=-20),),
        )
        saved = resolve_spell(
            Random(2), hold, slot_level=2, save_dc=15,
            targets=(SpellTarget(name="Free", save_modifier=+40),),
        )
        assert failed.results[0].condition_applied is Condition.PARALYZED
        assert saved.results[0].condition_applied is None

    def test_concentration_is_reported_for_concentration_spells(self) -> None:
        hold = spellbook()["Hold Person"]
        resolution = resolve_spell(
            Random(2), hold, slot_level=2, save_dc=15,
            targets=(SpellTarget(name="Held", save_modifier=-20),),
        )
        assert resolution.concentration_started
        assert not _fireball().concentration


class TestBundledData:
    def test_every_bundled_spell_declares_srd_provenance(self) -> None:
        assert all(spell.provenance == "SRD 5.2" for spell in spellbook().values())
