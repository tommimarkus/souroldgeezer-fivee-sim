"""Spell resolution tests.

The two orderings that matter: an area spell rolls its damage once for everyone
in it, and an attack-roll spell resolves the attack before its damage so a
critical can double the dice.
"""

from __future__ import annotations

from random import Random

import pytest

from fivee_sim.content import spellbook
from fivee_sim.kernel.conditions import Condition
from fivee_sim.kernel.dice import Advantage, Dice
from fivee_sim.kernel.rules import Ability, DamageType
from fivee_sim.kernel.spells import Spell, SpellTarget, resolve_spell

from .conftest import FixedRandom


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
        # Stated as two facts rather than "not affected *or* full damage": that
        # disjunction is satisfied by a target the spell stopped affecting at all,
        # so a regression that dropped the failing target would have passed it.
        assert failed.affected
        assert failed.damage_dealt == base
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


class TestSpellAttackAdvantage:
    """A spell attack roll carries Advantage the way a weapon attack roll does.

    SRD 5.2.1 Rules Glossary, "Attack Roll": "An attack roll is a D20 Test that
    represents making an attack with a weapon, an Unarmed Strike, or a spell."
    Advantage is a property of the d20 test, not of the thing swung, so nothing
    about a spell exempts it.
    """

    def bolt(self) -> Spell:
        return spellbook()["Guiding Bolt"]

    def test_a_target_carrying_advantage_rolls_two_dice(self) -> None:
        resolution = resolve_spell(
            Random(5),
            self.bolt(),
            slot_level=1,
            save_dc=15,
            spell_attack_bonus=5,
            targets=(
                SpellTarget(name="Held", ac=15, attack_advantage=Advantage.ADVANTAGE),
            ),
        )
        attack = resolution.results[0].attack
        assert attack is not None
        assert attack.roll.advantage is Advantage.ADVANTAGE
        assert len(attack.roll.rolls) == 2

    def test_advantage_is_decided_per_target_rather_than_per_cast(self) -> None:
        # The reason these live on SpellTarget rather than on the call: one spell
        # can strike several creatures in different states, and a single
        # spell-wide value cannot describe all three of these at once.
        resolution = resolve_spell(
            Random(5),
            self.bolt(),
            slot_level=1,
            save_dc=15,
            spell_attack_bonus=5,
            targets=(
                SpellTarget(name="Held", ac=15, attack_advantage=Advantage.ADVANTAGE),
                SpellTarget(name="Alert", ac=15),
                SpellTarget(
                    name="Dodging", ac=15, attack_advantage=Advantage.DISADVANTAGE
                ),
            ),
        )
        held, alert, dodging = (result.attack for result in resolution.results)
        assert held is not None and alert is not None and dodging is not None
        assert held.roll.advantage is Advantage.ADVANTAGE
        assert alert.roll.advantage is Advantage.NONE
        assert dodging.roll.advantage is Advantage.DISADVANTAGE

    def test_a_forced_critical_upgrades_a_hit(self) -> None:
        resolution = resolve_spell(
            FixedRandom(15),
            self.bolt(),
            slot_level=1,
            save_dc=15,
            spell_attack_bonus=5,
            targets=(SpellTarget(name="Held", ac=15, forced_critical=True),),
        )
        result = resolution.results[0]
        assert result.attack is not None
        assert result.attack.hit
        assert result.attack.critical
        # Guiding Bolt is 4d6, doubled to 8d6, every die maximised by the fixture.
        assert result.damage_dealt == 8 * 6

    def test_a_forced_critical_does_not_turn_a_miss_into_a_hit(self) -> None:
        resolution = resolve_spell(
            FixedRandom(2),
            self.bolt(),
            slot_level=1,
            save_dc=15,
            spell_attack_bonus=0,
            targets=(SpellTarget(name="Missed", ac=25, forced_critical=True),),
        )
        result = resolution.results[0]
        assert result.attack is not None
        assert not result.attack.hit
        assert not result.attack.critical
        assert result.damage_dealt == 0

    def test_a_forced_critical_is_decided_per_target_too(self) -> None:
        # Two creatures caught by one spell, one within 5 ft of the caster and one
        # not. The automatic critical is scoped by that distance, so it cannot be
        # a property of the cast.
        resolution = resolve_spell(
            FixedRandom(15),
            self.bolt(),
            slot_level=1,
            save_dc=15,
            spell_attack_bonus=5,
            targets=(
                SpellTarget(name="Adjacent", ac=15, forced_critical=True),
                SpellTarget(name="Distant", ac=15, forced_critical=False),
            ),
        )
        adjacent, distant = (result.attack for result in resolution.results)
        assert adjacent is not None and distant is not None
        assert adjacent.hit and adjacent.critical
        assert distant.hit and not distant.critical


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
        # Compared by value, not identity: a condition is a name, and one loaded from
        # a pack is an ordinary string rather than a Condition member.
        assert failed.results[0].condition_applied == Condition.PARALYZED
        assert saved.results[0].condition_applied is None

    def test_concentration_is_reported_for_concentration_spells(self) -> None:
        hold = spellbook()["Hold Person"]
        resolution = resolve_spell(
            Random(2), hold, slot_level=2, save_dc=15,
            targets=(SpellTarget(name="Held", save_modifier=-20),),
        )
        assert resolution.concentration_started
        assert not _fireball().concentration


class TestHealingScalesWithTheCaster:
    """The one thing a damaging spell never does: read the caster's own ability.

    SRD 5.2.1 Cure Wounds heals ``2d8 plus your spellcasting ability modifier``,
    so one record heals different amounts in two casters' hands. ``Spell.heal``
    is a fixed :class:`Dice` shared by everyone who knows the spell, so the
    modifier has to arrive at resolution or not at all.

    Every case here asserts a *difference* between two resolutions at one seed
    rather than a total. The dice are then identical in both, so the modifier is
    the only thing the assertion can be measuring — and a change to the healing
    dice cannot make a broken modifier pass.
    """

    def _ally(self) -> tuple[SpellTarget, ...]:
        return (SpellTarget(name="Ally"),)

    def test_the_casters_modifier_is_added_to_the_healing(self) -> None:
        cure = spellbook()["Cure Wounds"]
        without = resolve_spell(
            Random(7), cure, slot_level=1, save_dc=13, targets=self._ally()
        )
        with_mod = resolve_spell(
            Random(7), cure, slot_level=1, save_dc=13,
            spellcasting_modifier=3, targets=self._ally(),
        )

        assert with_mod.results[0].healed == without.results[0].healed + 3

    def test_upcasting_scales_the_dice_but_adds_the_modifier_once(self) -> None:
        # The modifier is not per slot level. Measured at slot 3, where a
        # per-level mistake would show up as +12 rather than +4.
        cure = spellbook()["Cure Wounds"]
        without = resolve_spell(
            Random(5), cure, slot_level=3, save_dc=13, targets=self._ally()
        )
        with_mod = resolve_spell(
            Random(5), cure, slot_level=3, save_dc=13,
            spellcasting_modifier=4, targets=self._ally(),
        )

        assert with_mod.results[0].healed == without.results[0].healed + 4

    def test_a_spell_that_does_not_declare_it_ignores_the_modifier(self) -> None:
        # Opt-in, so a pack's healing spell that transcribes a flat number keeps
        # healing that number whoever casts it.
        flat = Spell(name="Vale Balm", level=1, heal=Dice.parse("2d4"), range_feet=30)
        without = resolve_spell(
            Random(9), flat, slot_level=1, save_dc=13, targets=self._ally()
        )
        with_mod = resolve_spell(
            Random(9), flat, slot_level=1, save_dc=13,
            spellcasting_modifier=5, targets=self._ally(),
        )

        assert with_mod.results[0].healed == without.results[0].healed

    def test_a_negative_modifier_never_heals_a_negative_amount(self) -> None:
        # A caster with a penalty is unusual but expressible, and healing for a
        # negative number would drain the target on a low roll.
        cure = spellbook()["Cure Wounds"]
        resolution = resolve_spell(
            Random(4), cure, slot_level=1, save_dc=13,
            spellcasting_modifier=-20, targets=self._ally(),
        )

        assert resolution.results[0].healed >= 0


class TestTempHpGrantingSpells:
    """SRD 5.2.1, Temporary Hit Points, on a spell — never routed through
    ``heal``, mirroring it exactly otherwise: rolled once, shared by every
    target in the area, and scaled by ``upcast_temp_hp`` the same way
    ``upcast_heal`` scales healing.
    """

    def test_temp_hp_is_rolled_once_and_shared_by_every_target(self) -> None:
        spell = Spell(
            name="Warding Chant",
            level=1,
            temp_hp=Dice.parse("2d6+2"),
            range_feet=30,
            max_targets=3,
            provenance="synthetic test fixture, not SRD content",
        )
        resolution = resolve_spell(
            Random(6),
            spell,
            slot_level=1,
            save_dc=13,
            targets=(SpellTarget(name="Ally A"), SpellTarget(name="Ally B")),
        )
        assert resolution.temp_hp_roll is not None
        first, second = resolution.results
        assert first.temp_hp_granted == resolution.temp_hp_roll.total
        assert second.temp_hp_granted == resolution.temp_hp_roll.total
        assert first.healed == 0

    def test_upcasting_scales_the_temp_hp_dice(self) -> None:
        spell = Spell(
            name="Warding Chant",
            level=1,
            temp_hp=Dice.parse("1d6"),
            upcast_temp_hp=Dice.parse("1d6"),
            range_feet=30,
            provenance="synthetic test fixture, not SRD content",
        )
        assert str(spell.temp_hp_at(1)) == "1d6"
        assert str(spell.temp_hp_at(3)) == "3d6"

    def test_a_grant_alone_is_not_healing(self) -> None:
        spell = Spell(
            name="Warding Chant",
            level=1,
            temp_hp=Dice.parse("2d4"),
            range_feet=30,
            provenance="synthetic test fixture, not SRD content",
        )
        resolution = resolve_spell(
            Random(2), spell, slot_level=1, save_dc=13,
            targets=(SpellTarget(name="Ally"),),
        )
        assert resolution.healing_roll is None
        assert resolution.results[0].healed == 0
        assert resolution.results[0].temp_hp_granted > 0
        assert resolution.results[0].affected


class TestBundledData:
    def test_every_bundled_spell_declares_srd_provenance(self) -> None:
        assert all(spell.provenance == "SRD 5.2.1" for spell in spellbook().values())

    def test_cure_wounds_is_touch_range_rather_than_unbounded(self) -> None:
        # ``range_feet: 0`` means *no range check at all* in the stepper, so a
        # touch spell that omitted its range would heal across the battlefield.
        cure = spellbook()["Cure Wounds"]

        assert cure.range_feet == 5
        assert cure.heal == Dice.parse("2d8")
        assert cure.upcast_heal == Dice.parse("2d8")
        assert cure.add_spellcasting_modifier
