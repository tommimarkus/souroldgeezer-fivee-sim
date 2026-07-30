"""Encounter tests: initiative, turns, damage, reactions, spell resources.

Because the generator is passed to each call rather than held by the encounter,
these tests build a fight with an ordinary seed and then resolve a specific action
with a forced generator. That is how a single attack's outcome gets pinned without
contriving the whole fight.
"""

from __future__ import annotations

from collections.abc import Sequence
from random import Random

import pytest

from fivee_sim.data import make_monster, spellbook
from fivee_sim.kernel.actions import AttackKind
from fivee_sim.kernel.conditions import Condition
from fivee_sim.kernel.dice import Advantage, Dice
from fivee_sim.kernel.items import ItemEffect
from fivee_sim.kernel.rules import Ability, DamageType
from fivee_sim.kernel.spells import Spell
from fivee_sim.model.creature import AttackOption, Creature
from fivee_sim.model.encounter import (
    Action,
    ActionKind,
    Encounter,
    EncounterError,
    Event,
)

from .test_kernel import FixedRandom, ScriptedRandom

FIXTURE = "synthetic test fixture, not SRD content"


def fighter(
    name: str = "Thora",
    *,
    position: int = 0,
    hp: int | None = None,
    max_hp: int = 30,
    team: str = "party",
    attacks_per_action: int = 1,
) -> Creature:
    return Creature(
        name=name,
        team=team,
        ac=16,
        max_hp=max_hp,
        hp=max_hp if hp is None else hp,
        speed=30,
        abilities={
            Ability.STRENGTH: 16,
            Ability.DEXTERITY: 14,
            Ability.CONSTITUTION: 14,
            Ability.INTELLIGENCE: 10,
            Ability.WISDOM: 12,
            Ability.CHARISMA: 8,
        },
        attacks=(
            AttackOption(
                name="Longsword",
                attack_bonus=5,
                damage=Dice(1, 8, 3),
                damage_type=DamageType.SLASHING,
                kind=AttackKind.MELEE,
                provenance=FIXTURE,
            ),
        ),
        attacks_per_action=attacks_per_action,
        position=position,
        provenance=FIXTURE,
    )


def caster(name: str = "Wren", *, position: int = 0, team: str = "party") -> Creature:
    return Creature(
        name=name,
        team=team,
        ac=13,
        max_hp=24,
        speed=30,
        abilities={
            Ability.CONSTITUTION: 14,
            Ability.DEXTERITY: 12,
            Ability.INTELLIGENCE: 16,
        },
        spells=("Fireball", "Hold Person"),
        spell_slots={2: 1, 3: 1},
        spell_save_dc=15,
        spell_attack_bonus=6,
        position=position,
        provenance=FIXTURE,
    )


def advance_to(encounter: Encounter, name: str, rng: Random, limit: int = 24) -> None:
    for _ in range(limit):
        if encounter.current_name == name:
            return
        encounter.advance(rng)
    raise AssertionError(f"{name} never got a turn")


def kinds(events: Sequence[Event]) -> list[str]:
    return [event.kind for event in events]


def detail_of(events: Sequence[Event], kind: str) -> str:
    """The detail of the one event of ``kind``, asserting there is exactly one."""
    matching = [event for event in events if event.kind == kind]
    assert len(matching) == 1, f"expected one {kind!r} event, got {len(matching)}"
    return matching[0].detail


class TestInitiative:
    def test_the_same_seed_produces_the_same_order(self) -> None:
        first = Encounter([fighter(), make_monster("Wolf")], Random(7))
        second = Encounter([fighter(), make_monster("Wolf")], Random(7))
        assert first.order == second.order

    def test_ties_break_on_name_when_dexterity_matches(self) -> None:
        # A forced generator gives everyone the same d20, and identical Dexterity
        # leaves only the name to separate them — never randomness.
        encounter = Encounter(
            [fighter("Bravo", team="a"), fighter("Alpha", team="b")],
            FixedRandom(10),
        )
        assert encounter.order == ["Alpha", "Bravo"]

    def test_an_encounter_needs_two_combatants(self) -> None:
        with pytest.raises(EncounterError, match="at least two"):
            Encounter([fighter()], Random(1))

    def test_duplicate_names_are_refused(self) -> None:
        with pytest.raises(EncounterError, match="unique"):
            Encounter([fighter("Same"), fighter("Same", team="b")], Random(1))


class TestAttacking:
    def test_a_hit_reduces_hit_points(self) -> None:
        rng = Random(3)
        target = make_monster("Ogre", label="Ogre", position=5)
        encounter = Encounter([fighter(), target], rng)
        advance_to(encounter, "Thora", rng)
        before = target.hp
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Ogre"), FixedRandom(20)
        )
        assert "attack" in kinds(events)
        assert target.hp < before

    def test_an_attack_beyond_reach_does_not_consume_the_attack(self) -> None:
        rng = Random(3)
        far = make_monster("Ogre", label="Ogre", position=60)
        encounter = Encounter([fighter(), far], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.ATTACK, target="Ogre"), rng)
        assert "cannot reach" in events[0].detail
        assert far.hp == far.max_hp

    def test_extra_attack_allows_a_second_swing_but_not_a_third(self) -> None:
        rng = Random(5)
        target = make_monster("Ogre", label="Ogre", position=5)
        encounter = Encounter([fighter(attacks_per_action=2), target], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Ogre"), FixedRandom(20))
        encounter.act(Action(kind=ActionKind.ATTACK, target="Ogre"), FixedRandom(20))
        with pytest.raises(EncounterError, match="no attacks left"):
            encounter.act(Action(kind=ActionKind.ATTACK, target="Ogre"), FixedRandom(20))

    def test_dodging_imposes_disadvantage_on_incoming_attacks(self) -> None:
        rng = Random(2)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=5)
        encounter = Encounter([fighter(), goblin], rng)
        advance_to(encounter, "Goblin", rng)
        encounter.act(Action(kind=ActionKind.DODGE), rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.ATTACK, target="Goblin"), Random(4))
        assert "disadvantage" in events[0].detail

    def test_a_shot_from_5_feet_at_a_prone_target_has_advantage(self) -> None:
        """SRD 5.2 Rules Glossary, Prone, "Attacks Affected": "An attack roll
        against you has Advantage if the attacker is within 5 feet of you.
        Otherwise, that attack roll has Disadvantage."

        The clause names a distance and no weapon, exactly as the
        Paralyzed/Unconscious automatic critical does. The engine used to gate it on
        ``AttackKind``, so a bow loosed point-blank at a Prone creature came out
        with Disadvantage where the rule gives Advantage.
        """
        rng = Random(2)
        archer = fighter("Archer")
        shortbow = AttackOption(
            name="Shortbow",
            attack_bonus=5,
            damage=Dice(1, 6, 2),
            damage_type=DamageType.PIERCING,
            kind=AttackKind.RANGED,
            normal_range=80,
            long_range=320,
            provenance=FIXTURE,
        )
        archer.attacks = (shortbow,)
        target = fighter("Mark", team="foes", position=5)
        target.add_condition(Condition.PRONE)
        encounter = Encounter([archer, target], rng)
        assert (
            encounter.attack_advantage(archer, target, shortbow) is Advantage.ADVANTAGE
        )
        # The other half of the same clause is likewise the distance: the same bow
        # from across the room still gets Disadvantage, and no long-range penalty is
        # in play at 60 ft to confuse the reading.
        target.position = 60
        assert (
            encounter.attack_advantage(archer, target, shortbow)
            is Advantage.DISADVANTAGE
        )

    def test_a_reach_weapon_beyond_5_feet_gets_the_prone_disadvantage(self) -> None:
        # The mirror case, and the one the old gate got right by accident: a melee
        # attack made from beyond 5 feet is not "within 5 feet of you" either.
        rng = Random(2)
        pikeman = fighter("Pikeman")
        pike = AttackOption(
            name="Pike",
            attack_bonus=5,
            damage=Dice(1, 10, 3),
            damage_type=DamageType.PIERCING,
            kind=AttackKind.MELEE,
            reach=10,
            provenance=FIXTURE,
        )
        pikeman.attacks = (pike,)
        target = fighter("Mark", team="foes", position=10)
        target.add_condition(Condition.PRONE)
        encounter = Encounter([pikeman, target], rng)
        assert (
            encounter.attack_advantage(pikeman, target, pike) is Advantage.DISADVANTAGE
        )

    def test_unknown_attack_name_is_reported_with_the_options(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="Longsword"):
            encounter.act(
                Action(kind=ActionKind.ATTACK, target="Wolf", attack="Halberd"), rng
            )


class TestGoingDown:
    def test_reaching_zero_knocks_a_creature_out_rather_than_killing_it(self) -> None:
        rng = Random(3)
        victim = fighter("Victim", team="foes", max_hp=40, hp=1, position=5)
        encounter = Encounter([fighter(), victim], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(20))
        assert victim.hp == 0
        assert not victim.dead
        assert victim.dying
        assert Condition.UNCONSCIOUS in victim.conditions
        assert Condition.PRONE in victim.conditions

    def test_damage_exceeding_maximum_hit_points_kills_outright(self) -> None:
        victim = fighter("Victim", team="foes", max_hp=4, hp=4)
        victim.take_damage(20)
        assert victim.dead
        assert not victim.dying

    def test_death_saves_are_rolled_at_the_start_of_a_dying_turn(self) -> None:
        # A third combatant keeps the fight alive. In a duel, dropping the only
        # opponent ends the encounter and advance() stops, so the dying creature
        # would never get the turn on which it would roll.
        rng = Random(8)
        victim = fighter("Victim", team="foes", max_hp=40, hp=1, position=5)
        ally = make_monster("Wolf", label="Wolf", team="foes", position=10)
        encounter = Encounter([fighter(), victim, ally], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(20))
        assert victim.dying
        assert not encounter.over

        for _ in range(12):
            events = encounter.advance(rng)
            if encounter.current_name == "Victim":
                assert "death_save" in kinds(events)
                return
        raise AssertionError("the dying creature never took a turn")

    @staticmethod
    def _dying_hero() -> tuple[Encounter, Creature]:
        """A fight whose Hero is at 0 hit points, paused on the attacker's turn."""
        rng = Random(8)
        hero = fighter("Hero", max_hp=30, hp=1, position=0)
        foe = fighter("Foe", team="foes", position=5)
        ally = fighter("Ally", position=40)  # keeps the fight from ending
        encounter = Encounter([hero, foe, ally], rng)
        advance_to(encounter, "Foe", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Hero"), FixedRandom(20))
        assert hero.dying
        return encounter, hero

    def test_stabilising_clears_both_death_save_counters(self) -> None:
        """SRD 5.2, "Playing the Game" -> "Death Saving Throws", Three
        Successes/Failures: "The successes and failures don't need to be
        consecutive; keep track of both until you collect three of a kind. The
        number of both is reset to zero when you regain any Hit Points or become
        Stable."

        ``Creature.heal`` already honours the first half. The roll that stabilises
        set ``stable`` and left the counters standing.
        """
        encounter, hero = self._dying_hero()
        hero.death_save_successes = 2
        hero.death_save_failures = 1
        # A forced 15 succeeds: the third success, which stabilises.
        advance_to(encounter, "Hero", FixedRandom(15))
        assert hero.stable
        assert hero.death_save_successes == 0
        assert hero.death_save_failures == 0

    def test_a_stabilised_creature_knocked_down_again_starts_from_nothing(
        self,
    ) -> None:
        # What the stale counters bought: with three successes still on the sheet, a
        # *failed* death save re-stabilised the creature. The failure took it to two,
        # short of the three that kill, and the untouched successes then tripped the
        # stabilise branch immediately below.
        encounter, hero = self._dying_hero()
        hero.death_save_successes = 2
        advance_to(encounter, "Hero", FixedRandom(15))
        assert hero.stable

        hero.take_damage(3)
        assert not hero.stable
        assert hero.death_save_successes == 0
        assert hero.death_save_failures == 1

        # A forced 5 fails. It must not stabilise anything.
        encounter.advance(FixedRandom(5))
        advance_to(encounter, "Hero", FixedRandom(5))
        assert not hero.stable
        assert hero.death_save_failures == 2
        assert hero.death_save_successes == 0

    def test_healing_from_zero_clears_unconsciousness_and_resets_saves(self) -> None:
        victim = fighter("Victim", max_hp=20, hp=1)
        victim.take_damage(1)
        victim.death_save_failures = 2
        victim.heal(5)
        assert victim.hp == 5
        assert Condition.UNCONSCIOUS not in victim.conditions
        assert victim.death_save_failures == 0


class TestDamageAtZeroHitPoints:
    """Damage taken *while already* at 0 hit points is its own rule.

    SRD 5.2, "Damage at 0 Hit Points": any damage costs a death saving throw
    failure, a critical hit costs two, and damage equalling or exceeding the hit
    point maximum kills outright. Nothing there resets the counters — only
    regaining hit points or becoming stable does that — so these tests are what
    keep the drop-to-0 reset from being applied a second time to a creature that
    was already down.
    """

    @staticmethod
    def _downed(failures: int = 0, successes: int = 0, max_hp: int = 30) -> Creature:
        victim = fighter("Victim", max_hp=max_hp, hp=1)
        victim.take_damage(1)
        assert victim.dying
        victim.death_save_failures = failures
        victim.death_save_successes = successes
        return victim

    def test_damage_while_down_costs_one_failure_and_keeps_the_rest(self) -> None:
        victim = self._downed(failures=1, successes=2)
        victim.take_damage(3)
        assert victim.hp == 0
        assert victim.death_save_failures == 2
        # Successes survive: only healing or stabilising resets them.
        assert victim.death_save_successes == 2
        assert victim.dying and not victim.dead

    def test_a_critical_hit_while_down_costs_two_failures(self) -> None:
        victim = self._downed()
        victim.take_damage(3, critical=True)
        assert victim.death_save_failures == 2
        assert victim.dying and not victim.dead

    def test_a_third_failure_from_damage_kills(self) -> None:
        victim = self._downed(failures=2)
        victim.take_damage(3)
        assert victim.dead
        assert not victim.dying
        # The rolled-failure death path discards unconsciousness; so must this one.
        assert Condition.UNCONSCIOUS not in victim.conditions

    def test_a_critical_hit_finishes_a_creature_that_has_failed_once(self) -> None:
        victim = self._downed(failures=1)
        victim.take_damage(3, critical=True)
        assert victim.dead

    def test_damage_equal_to_the_maximum_kills_outright_rather_than_by_failure(
        self,
    ) -> None:
        victim = self._downed(max_hp=30)
        victim.take_damage(30)
        assert victim.dead
        # Killed by the massive-damage rule, so no failure was ever accrued.
        assert victim.death_save_failures == 0
        assert Condition.UNCONSCIOUS not in victim.conditions

    def test_damage_ends_stability_and_still_costs_a_failure(self) -> None:
        # A stable creature has 0 hit points, so both rules apply at once: it stops
        # being stable *and* it takes the failure.
        victim = self._downed()
        victim.stable = True
        victim.take_damage(3)
        assert not victim.stable
        assert victim.death_save_failures == 1
        assert victim.dying

    def test_dropping_to_zero_still_clears_the_counters(self) -> None:
        # The behaviour the fix must not regress: the *drop* is a fresh dying state.
        victim = fighter("Victim", max_hp=30, hp=10)
        victim.death_save_failures = 2
        victim.death_save_successes = 1
        victim.take_damage(10)
        assert victim.hp == 0
        assert victim.death_save_failures == 0
        assert victim.death_save_successes == 0
        assert Condition.UNCONSCIOUS in victim.conditions
        assert Condition.PRONE in victim.conditions

    def test_damage_to_a_corpse_changes_nothing(self) -> None:
        victim = self._downed(failures=2)
        victim.take_damage(3)
        assert victim.dead
        victim.take_damage(3)
        assert victim.dead
        assert victim.death_save_failures == 3
        assert Condition.UNCONSCIOUS not in victim.conditions

    @staticmethod
    def _fight_with_a_dying_creature(
        item: ItemEffect, *, max_hp: int = 30
    ) -> tuple[Encounter, Creature]:
        """A fight paused on Thug's turn, with Victim at 0 hit points.

        A third combatant keeps the encounter running — dropping the only opponent
        would end it, and ``advance`` would stop. Every death save on the way back
        round is forced to 15, a success, so the victim can neither die nor
        stabilise before the item lands.
        """
        rng = Random(11)
        thug = fighter("Thug", team="foes", position=0)
        thug.items = {"Vial": 2}
        victim = fighter("Victim", max_hp=max_hp, hp=1, position=5)
        ally = fighter("Ally", position=40)
        encounter = Encounter([thug, victim, ally], rng, items={"Vial": item})
        advance_to(encounter, "Thug", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(20))
        assert victim.dying
        encounter.advance(FixedRandom(15))
        advance_to(encounter, "Thug", FixedRandom(15))
        return encounter, victim

    @staticmethod
    def _throw_the_vial(encounter: Encounter) -> list[Event]:
        return encounter.act(
            Action(kind=ActionKind.USE_ITEM, item="Vial", target="Victim"), Random(3)
        )

    def test_a_third_failure_from_damage_is_announced_as_a_death(self) -> None:
        """Killing an already-unconscious creature has to narrate.

        ``_apply_damage`` decided whether to announce anything from
        ``was_conscious and not conscious``, which is false for a creature that was
        already at 0. So the creature died, the state said so, and the log said
        nothing — the one event a narrator most needs.
        """
        fire = ItemEffect(
            damage=Dice.parse("2d6"),
            damage_type=DamageType.FIRE,
            save_ability=Ability.DEXTERITY,
            save_dc=13,
            provenance=FIXTURE,
        )
        encounter, victim = self._fight_with_a_dying_creature(fire)
        victim.death_save_failures = 2
        events = self._throw_the_vial(encounter)
        assert victim.dead
        assert victim.death_save_failures == 3
        assert "death" in kinds(events)
        assert "failed death save" in detail_of(events, "death")

    def test_massive_damage_to_a_dying_creature_is_announced_as_a_death(self) -> None:
        # The second route to ``dead`` from 0 hit points, and it was equally silent:
        # damage at 0 that equals or exceeds the maximum kills without ever
        # accruing a failure.
        bomb = ItemEffect(
            damage=Dice(4, 6, 40), damage_type=DamageType.FIRE, provenance=FIXTURE
        )
        encounter, victim = self._fight_with_a_dying_creature(bomb, max_hp=25)
        events = self._throw_the_vial(encounter)
        assert victim.dead
        # Killed by the massive-damage rule, so no failure was accrued...
        assert victim.death_save_failures == 0
        assert "death" in kinds(events)
        # ...and the narration says which rule did it.
        assert detail_of(events, "death") == "damage exceeded maximum hit points"

    def test_a_creature_that_survives_the_hit_is_not_announced_dead(self) -> None:
        # The guard on the fix: a dying creature that merely takes another failure
        # still produces no death event.
        fire = ItemEffect(
            damage=Dice.parse("2d6"),
            damage_type=DamageType.FIRE,
            save_ability=Ability.DEXTERITY,
            save_dc=13,
            provenance=FIXTURE,
        )
        encounter, victim = self._fight_with_a_dying_creature(fire)
        events = self._throw_the_vial(encounter)
        assert victim.dying and not victim.dead
        assert "death" not in kinds(events)
        assert "down" not in kinds(events)

    def test_a_damaging_item_on_a_dying_creature_costs_a_failure(self) -> None:
        # An item was once the *only* route to this rule: an attack refused an
        # unconscious target and a spell filtered the area down to conscious
        # creatures, while an item only ever refused a corpse. Attacks and spells
        # now reach it too — see ``TestADownedCreatureIsStillATarget`` — so this
        # covers the item path rather than standing in for all three.
        fire = ItemEffect(
            damage=Dice.parse("2d6"),
            damage_type=DamageType.FIRE,
            save_ability=Ability.DEXTERITY,
            save_dc=13,
            provenance=FIXTURE,
        )
        rng = Random(11)
        thug = fighter("Thug", team="foes", position=0)
        thug.items = {"Alchemist's Fire": 2}
        victim = fighter("Victim", max_hp=30, hp=1, position=5)
        ally = fighter("Ally", position=40)  # a third combatant keeps the fight alive
        encounter = Encounter(
            [thug, victim, ally], rng, items={"Alchemist's Fire": fire}
        )

        def death_saves() -> dict[str, int]:
            for row in encounter.state()["combatants"]:
                if row["name"] == "Victim":
                    saves: dict[str, int] = row["death_saves"]
                    return saves
            raise AssertionError("Victim is not in the state")

        advance_to(encounter, "Thug", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(20))
        assert victim.dying

        # Accrue one real failure: a forced natural 5 fails every death save.
        for _ in range(12):
            encounter.advance(FixedRandom(5))
            if victim.death_save_failures:
                break
        assert death_saves() == {"successes": 0, "failures": 1}

        # A forced 15 succeeds, so reaching the thug's turn cannot add a failure
        # and cannot reach the three successes that would stabilise.
        advance_to(encounter, "Thug", FixedRandom(15))
        before = death_saves()
        events = encounter.act(
            Action(kind=ActionKind.USE_ITEM, item="Alchemist's Fire", target="Victim"),
            Random(3),
        )
        after = death_saves()

        assert "damage" in kinds(events)
        assert victim.hp == 0
        assert after["failures"] == before["failures"] + 1
        assert after["successes"] == before["successes"]
        assert victim.dying and not victim.dead


class TestADownedCreatureIsStillATarget:
    """A creature at 0 hit points is a legal target; only a corpse is not.

    SRD 5.2, Rules Glossary, "Unconscious [Condition]": "Attacks Affected. Attack
    rolls against you have Advantage." and "Automatic Critical Hits. Any attack
    roll that hits you is a Critical Hit if the attacker is within 5 feet of you."
    Both clauses are dead text if the stepper refuses the attack, which is what it
    used to do — and the Unconscious clause "Saving Throws Affected. You
    automatically fail Strength and Dexterity saving throws" is likewise dead if an
    area effect filters the creature out before rolling one.

    What the damage then costs is the other rule. SRD 5.2, "Playing the Game" ->
    "Damage at 0 Hit Points": "If you take any damage while you have 0 Hit Points,
    you suffer a Death Saving Throw failure. If the damage is from a Critical Hit,
    you suffer two failures instead. If the damage equals or exceeds your Hit Point
    maximum, you die."

    The hit point maximums here are deliberately far above anything the fixtures
    can roll, because the massive-damage clause is checked first: a fixture small
    enough to die would pin instant death rather than the failure count. The one
    test that *wants* that ordering sizes itself to reach it.
    """

    @staticmethod
    def _paused_on_the_attackers_turn(
        attacker: Creature, victim: Creature, *others: Creature
    ) -> Encounter:
        """A fight held on ``attacker``'s turn, with a third combatant to sustain it.

        The ally exists because ``Encounter.over`` counts only conscious creatures:
        without it, dropping the victim would end the fight and every action after
        would be refused for that reason rather than the one under test. It stands
        500 ft away so nothing under test can reach it.
        """
        rng = Random(8)
        ally = fighter("Ally", team=victim.team, position=500)
        encounter = Encounter(
            [attacker, victim, ally, *others], rng, spellbook=spellbook()
        )
        advance_to(encounter, attacker.name, rng)
        return encounter

    @staticmethod
    def _archer(name: str = "Archer", *, team: str = "foes") -> Creature:
        archer = fighter(name, team=team, position=0)
        archer.attacks = (
            AttackOption(
                name="Shortbow",
                attack_bonus=5,
                damage=Dice(1, 6, 2),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.RANGED,
                normal_range=80,
                long_range=320,
                provenance=FIXTURE,
            ),
        )
        return archer

    def test_the_unconscious_condition_reaches_an_attack_on_a_downed_target(
        self,
    ) -> None:
        # The condition table already carried both clauses; nothing could consult
        # them, because the only target they apply to was refused outright.
        thug = fighter("Thug", team="foes", position=0)
        victim = fighter("Victim", max_hp=200, hp=1, position=5)
        encounter = self._paused_on_the_attackers_turn(thug, victim)
        victim.take_damage(1)
        assert victim.dying

        option = thug.attacks[0]
        assert encounter.attack_advantage(thug, victim, option) is Advantage.ADVANTAGE
        assert encounter.attack_forced_critical(thug, victim) is True
        # The critical is scoped by distance, so it lapses out of melee while the
        # Advantage from Unconscious does not.
        victim.position = 30
        assert encounter.attack_forced_critical(thug, victim) is False

    def test_an_attack_on_a_dying_creature_lands_and_costs_one_failure(self) -> None:
        archer = self._archer()
        victim = fighter("Victim", max_hp=200, hp=1, position=30)
        encounter = self._paused_on_the_attackers_turn(archer, victim)
        victim.take_damage(1)
        assert victim.dying

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(19)
        )
        assert "damage" in kinds(events)
        assert victim.hp == 0
        assert victim.death_save_failures == 1
        assert victim.dying and not victim.dead
        # From 30 ft the hit is an ordinary one, which is the point of the range:
        # a melee swing would force the critical and cost two.
        assert "critical" not in detail_of(events, "attack")

    def test_a_critical_hit_on_a_dying_creature_costs_two_failures(self) -> None:
        thug = fighter("Thug", team="foes", position=0)
        victim = fighter("Victim", max_hp=200, hp=1, position=5)
        encounter = self._paused_on_the_attackers_turn(thug, victim)
        victim.take_damage(1)

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(19)
        )
        # Not a natural 20: the critical comes from the target's condition.
        assert "critical hit" in detail_of(events, "attack")
        assert victim.death_save_failures == 2
        assert victim.dying and not victim.dead

    def test_a_critical_reaching_the_maximum_kills_instead_of_costing_failures(
        self,
    ) -> None:
        # The ordering inside ``take_damage``: massive damage is checked before the
        # failure count, so a forced critical big enough to reach the maximum kills
        # outright and accrues nothing. A doubled 1d8+3 tops out at 19.
        thug = fighter("Thug", team="foes", position=0)
        victim = fighter("Victim", max_hp=19, hp=1, position=5)
        encounter = self._paused_on_the_attackers_turn(thug, victim)
        victim.take_damage(1)

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(19)
        )
        assert victim.dead
        assert victim.death_save_failures == 0
        assert detail_of(events, "death") == "damage exceeded maximum hit points"

    def test_three_failures_from_damage_kill_a_dying_creature(self) -> None:
        archer = self._archer()
        victim = fighter("Victim", max_hp=200, hp=1, position=30)
        encounter = self._paused_on_the_attackers_turn(archer, victim)
        victim.take_damage(1)
        victim.death_save_failures = 2

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(19)
        )
        assert victim.dead
        assert detail_of(events, "death") == "a third failed death save"

    def test_an_attack_on_a_stable_creature_starts_its_death_saves_again(self) -> None:
        """SRD 5.2, "Stabilizing a Character": "A Stable creature doesn't make Death
        Saving Throws even though it has 0 Hit Points, but it still has the
        Unconscious condition. If the creature takes damage, it stops being Stable
        and starts making Death Saving Throws again."

        ``Creature.take_damage`` already did this; nothing could deliver the damage
        by attack, because a Stable creature is not conscious either.
        """
        archer = self._archer()
        victim = fighter("Victim", max_hp=200, hp=1, position=30)
        encounter = self._paused_on_the_attackers_turn(archer, victim)
        victim.take_damage(1)
        victim.stable = True
        assert not victim.dying

        encounter.act(Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(19))
        assert not victim.stable
        assert victim.dying
        assert victim.death_save_failures == 1

    def test_a_corpse_is_refused_as_an_attack_target(self) -> None:
        thug = fighter("Thug", team="foes", position=0)
        victim = fighter("Victim", max_hp=30, hp=1, position=5)
        encounter = self._paused_on_the_attackers_turn(thug, victim)
        victim.take_damage(1)
        victim.dead = True

        with pytest.raises(EncounterError, match="dead"):
            encounter.act(
                Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(19)
            )

    def test_an_area_spell_centred_on_a_dying_creature_damages_it(self) -> None:
        wren = caster("Wren", team="foes", position=0)
        victim = fighter("Victim", max_hp=200, hp=1, position=30)
        # A second creature inside the blast: the old behaviour damaged this one and
        # left the dying creature at the exact point of origin untouched.
        standing = fighter("Standing", max_hp=200, position=35)
        encounter = self._paused_on_the_attackers_turn(wren, victim, standing)
        victim.take_damage(1)
        assert victim.dying
        assert encounter.auto_fails_save(victim, Ability.DEXTERITY) is True

        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=30),
            FixedRandom(1),
        )
        # Eight d6 forced to 1, and the dying creature fails the save automatically.
        assert victim.hp == 0
        assert victim.death_save_failures == 1
        assert standing.hp == standing.max_hp - 8
        touched = {event.target for event in events if event.kind == "spell_effect"}
        assert touched == {"Victim", "Standing"}

    def test_a_corpse_is_not_caught_in_an_area_spell(self) -> None:
        wren = caster("Wren", team="foes", position=0)
        victim = fighter("Victim", max_hp=30, hp=1, position=30)
        encounter = self._paused_on_the_attackers_turn(wren, victim)
        victim.take_damage(1)
        victim.dead = True
        # The ally sits at 500 ft; only the corpse is anywhere near the blast, so a
        # spell that still caught corpses would report an effect on it.
        with pytest.raises(EncounterError, match="no valid targets"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=30),
                FixedRandom(1),
            )

    def test_a_spell_attack_critical_on_a_dying_creature_costs_two_failures(
        self,
    ) -> None:
        # The cast path's own critical. ``_do_cast`` reads it off the per-target
        # attack roll the kernel already produced; without that, Guiding Bolt loosed
        # point-blank at a downed creature doubled its dice and still cost one.
        wren = caster("Wren", team="foes", position=0)
        wren.spells = ("Guiding Bolt",)
        wren.spell_slots = {1: 2}
        victim = fighter("Victim", max_hp=200, hp=1, position=5)
        encounter = self._paused_on_the_attackers_turn(wren, victim)
        victim.take_damage(1)

        events = encounter.act(
            Action(
                kind=ActionKind.CAST,
                spell="Guiding Bolt",
                slot_level=1,
                target="Victim",
            ),
            FixedRandom(19),
        )
        assert "critical hit" in detail_of(events, "spell_effect")
        assert victim.death_save_failures == 2
        assert victim.dying and not victim.dead

    def test_each_target_of_one_cast_carries_its_own_critical(self) -> None:
        """The critical is read per target, because the kernel rolls it per target.

        ``resolve_spell`` makes a separate attack roll for each name, and the forced
        critical is scoped by the distance from the caster to *that* creature — so a
        single spell-wide flag would be wrong in both directions. Two downed targets
        in one cast, one adjacent and one across the room, separate the two: the near
        one is a critical and costs two failures, the far one is an ordinary hit and
        costs one. No bundled spell names more than one target, so this needs a
        fixture spell.
        """
        twin = Spell(
            name="Twin Bolt",
            level=1,
            requires_attack_roll=True,
            damage=Dice(1, 6, 0),
            damage_type=DamageType.RADIANT,
            range_feet=120,
            max_targets=2,
            provenance=FIXTURE,
        )
        wren = caster("Wren", team="foes", position=0)
        wren.spells = ("Twin Bolt",)
        wren.spell_slots = {1: 2}
        near = fighter("Near", max_hp=200, hp=1, position=5)
        far = fighter("Far", max_hp=200, hp=1, position=60)
        rng = Random(8)
        ally = fighter("Ally", position=500)
        encounter = Encounter(
            [wren, near, far, ally], rng, spellbook={"Twin Bolt": twin}
        )
        advance_to(encounter, "Wren", rng)
        near.take_damage(1)
        far.take_damage(1)

        encounter.act(
            Action(
                kind=ActionKind.CAST,
                spell="Twin Bolt",
                slot_level=1,
                targets=("Near", "Far"),
            ),
            FixedRandom(19),
        )
        assert near.death_save_failures == 2
        assert far.death_save_failures == 1


class TestMovementAndReactions:
    def test_leaving_a_threatened_space_draws_an_opportunity_attack(self) -> None:
        rng = Random(6)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=5)
        encounter = Encounter([fighter(), goblin], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        assert "opportunity_attack" in kinds(events)

    def test_disengaging_first_prevents_the_opportunity_attack(self) -> None:
        rng = Random(6)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=5)
        encounter = Encounter([fighter(), goblin], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.DISENGAGE), rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        assert "opportunity_attack" not in kinds(events)

    def test_moving_further_than_the_remaining_speed_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=90)], rng)
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="movement"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=80), rng)

    def test_dash_buys_a_second_helping_of_movement(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=90)], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.DASH), rng)
        encounter.act(Action(kind=ActionKind.MOVE, to_position=60), rng)
        assert encounter.creatures["Thora"].position == 60

    def test_a_grappled_creature_cannot_move(self) -> None:
        rng = Random(1)
        held = fighter("Held", position=0)
        held.add_condition(Condition.GRAPPLED)
        encounter = Encounter([held, make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Held", rng)
        with pytest.raises(EncounterError, match="speed 0"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=20), rng)


class TestSpellcasting:
    def test_casting_spends_a_slot_of_the_chosen_level(self) -> None:
        rng = Random(4)
        wizard = caster()
        goblin = make_monster("Goblin Warrior", label="Goblin", position=30)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=30),
            Random(9),
        )
        assert wizard.spell_slots[3] == 0

    def test_casting_without_a_slot_is_refused(self) -> None:
        rng = Random(4)
        wizard = caster()
        wizard.spell_slots[3] = 0
        goblin = make_monster("Goblin Warrior", label="Goblin", position=30)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="no level 3 slots"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=30),
                rng,
            )

    def test_a_slot_below_the_spells_level_is_refused_before_anything_is_spent(
        self,
    ) -> None:
        """A refusal must cost nothing — not the slot, and not the action.

        The check that a slot can carry the spell lives in ``resolve_spell``, which
        runs after the action is marked used and the slot decremented. So the
        refusal used to arrive having already taken both, and as a bare
        ``ValueError`` that ``encounter_act`` does not catch — escaping the
        "illegal actions are refused with the reason" contract as an unhandled
        server error.
        """
        rng = Random(4)
        wizard = caster()
        goblin = make_monster("Goblin Warrior", label="Goblin", position=30)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="level 3 .* level 2 slot"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Fireball", slot_level=2, center=30),
                rng,
            )
        assert wizard.spell_slots == {2: 1, 3: 1}
        assert encounter.state()["turn_state"]["action_used"] is False
        # The turn is intact, so the legal cast still goes through.
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=30),
            Random(9),
        )
        assert wizard.spell_slots == {2: 1, 3: 0}

    def test_an_area_spell_catches_everyone_inside_its_radius(self) -> None:
        rng = Random(4)
        wizard = caster(position=0)
        near = make_monster("Goblin Warrior", label="Goblin A", position=100)
        also_near = make_monster("Goblin Warrior", label="Goblin B", position=110)
        far = make_monster("Goblin Warrior", label="Goblin C", position=300)
        encounter = Encounter([wizard, near, also_near, far], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=105),
            Random(2),
        )
        assert near.hp < near.max_hp
        assert also_near.hp < also_near.max_hp
        assert far.hp == far.max_hp

    def test_an_area_spell_cannot_be_dropped_beyond_its_range(self) -> None:
        # The point of origin is what the range applies to. This used to be checked
        # for no spell with a radius at all, so a 150 ft Fireball would land at any
        # distance whatsoever.
        rng = Random(4)
        wizard = caster(position=0)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=1000)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="beyond Fireball's 150 ft range"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=1000),
                Random(2),
            )

    def test_an_area_spell_named_at_a_target_out_of_range_is_refused(self) -> None:
        rng = Random(4)
        wizard = caster(position=0)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=1000)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="beyond Fireball's 150 ft range"):
            encounter.act(
                Action(
                    kind=ActionKind.CAST,
                    spell="Fireball",
                    slot_level=3,
                    targets=("Goblin",),
                ),
                Random(2),
            )

    def test_a_creature_at_the_far_edge_of_a_blast_does_not_refuse_the_whole_spell(
        self,
    ) -> None:
        # The origin is in range; a creature caught 20 ft further out is not, and
        # must not veto a legal cast. This is why the range check is on the origin
        # rather than on each creature the radius sweeps up.
        rng = Random(4)
        wizard = caster(position=0)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=160)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=150),
            Random(2),
        )
        assert goblin.hp < goblin.max_hp

    def test_an_area_spell_is_bounded_by_its_radius_not_by_max_targets(self) -> None:
        # Every bundled area spell leaves max_targets at its default of 1. Enforcing
        # that on an area would shrink a Fireball to a single creature.
        rng = Random(4)
        wizard = caster(position=0)
        goblins = [
            make_monster("Goblin Warrior", label=f"Goblin {letter}", position=100 + step)
            for step, letter in enumerate("ABC")
        ]
        encounter = Encounter([wizard, *goblins], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=101),
            Random(2),
        )
        assert all(goblin.hp < goblin.max_hp for goblin in goblins)

    def test_naming_more_targets_than_a_spell_allows_is_refused(self) -> None:
        # max_targets is a documented pack field. It used to be sliced with
        # max(cap, len(named)), which can never truncate, so it did nothing at all.
        rng = Random(4)
        priest = caster(position=0)
        priest.spells = ("Guiding Bolt",)
        priest.spell_slots = {1: 4}
        goblins = [
            make_monster("Goblin Warrior", label=f"Goblin {letter}", position=20 + step)
            for step, letter in enumerate("AB")
        ]
        encounter = Encounter([priest, *goblins], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="at most 1 creature"):
            encounter.act(
                Action(
                    kind=ActionKind.CAST,
                    spell="Guiding Bolt",
                    slot_level=1,
                    targets=("Goblin A", "Goblin B"),
                ),
                Random(2),
            )

    def test_casting_an_unprepared_spell_is_refused(self) -> None:
        rng = Random(4)
        wizard = caster()
        encounter = Encounter(
            [wizard, make_monster("Wolf", position=20)], rng, spellbook=spellbook()
        )
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="does not have"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Shatter", targets=("Wolf",)), rng
            )

    def test_damage_forces_a_concentration_check(self) -> None:
        rng = Random(4)
        wizard = caster(position=0)
        wizard.concentrating_on = "Hold Person"
        goblin = make_monster("Goblin Warrior", label="Goblin", position=5)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Goblin", rng)
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Wren", attack="Scimitar"),
            FixedRandom(20),
        )
        assert "concentration" in kinds(events)

    def test_being_knocked_out_ends_concentration(self) -> None:
        wizard = caster()
        wizard.concentrating_on = "Hold Person"
        wizard.take_damage(wizard.hp)
        assert wizard.concentrating_on is None


class TestSavingThrowAdvantage:
    """A saving throw carries Advantage and Disadvantage the way an attack does.

    The rule these pin is that Restrained does *not* make a Dexterity save fail —
    it makes it hard. A Restrained creature caught in a Fireball still rolls, and
    can still take half damage, which an auto-fail flag makes impossible.
    """

    def fireball_save(
        self, *, conditions: Sequence[str] = (), dodging: bool = False
    ) -> Event:
        """Cast Fireball at a Goblin and return the event describing its save."""
        rng = Random(4)
        wizard = caster(position=0)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=30)
        for condition in conditions:
            goblin.add_condition(condition)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        if dodging:
            advance_to(encounter, "Goblin", rng)
            encounter.act(Action(kind=ActionKind.DODGE), rng)
        advance_to(encounter, "Wren", rng)
        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=30),
            Random(9),
        )
        return next(
            event
            for event in events
            if event.kind == "spell_effect" and event.target == "Goblin"
        )

    def test_a_restrained_target_saves_with_disadvantage_rather_than_failing(
        self,
    ) -> None:
        detail = self.fireball_save(conditions=(Condition.RESTRAINED,)).detail
        assert "disadvantage" in detail
        assert "auto-fail" not in detail

    def test_an_unhindered_target_saves_straight(self) -> None:
        detail = self.fireball_save().detail
        assert "disadvantage" not in detail
        assert "advantage" not in detail

    def test_a_paralyzed_target_still_fails_outright(self) -> None:
        assert "auto-fail" in self.fireball_save(conditions=(Condition.PARALYZED,)).detail

    def test_dodging_gives_advantage_on_a_dexterity_save(self) -> None:
        assert "advantage" in self.fireball_save(dodging=True).detail

    def test_a_restrained_dodger_loses_the_benefit_rather_than_cancelling(self) -> None:
        # Dodge's benefits are lost while Speed is 0, and Restrained sets Speed 0.
        # Treating the Dodge as a live source of Advantage would cancel the
        # Disadvantage and hand the creature a straight roll it has not earned.
        detail = self.fireball_save(
            conditions=(Condition.RESTRAINED,), dodging=True
        ).detail
        assert "disadvantage" in detail

    def test_a_forced_failure_and_disadvantage_are_decided_independently(self) -> None:
        rng = Random(4)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=30)
        goblin.add_condition(Condition.PARALYZED)
        goblin.add_condition(Condition.RESTRAINED)
        encounter = Encounter([caster(), goblin], rng, spellbook=spellbook())
        assert encounter.auto_fails_save(goblin, Ability.DEXTERITY)
        assert encounter.save_advantage(goblin, Ability.DEXTERITY) is Advantage.DISADVANTAGE

    def test_an_items_saving_throw_carries_it_too(self) -> None:
        fire = ItemEffect(
            damage=Dice.parse("2d6"),
            damage_type=DamageType.FIRE,
            save_ability=Ability.DEXTERITY,
            save_dc=13,
            provenance=FIXTURE,
        )
        rng = Random(11)
        thug = fighter("Thug", team="foes", position=0)
        thug.items = {"Alchemist's Fire": 1}
        victim = fighter("Victim", position=5)
        victim.add_condition(Condition.RESTRAINED)
        encounter = Encounter([thug, victim], rng, items={"Alchemist's Fire": fire})
        advance_to(encounter, "Thug", rng)
        events = encounter.act(
            Action(kind=ActionKind.USE_ITEM, item="Alchemist's Fire", target="Victim"),
            Random(6),
        )
        assert "disadvantage" in events[0].detail


class TestSpellAttackAdvantage:
    """The cast path reaches the same answer about Advantage as the swing path.

    SRD 5.2 Rules Glossary, "Attack Roll": "An attack roll is a D20 Test that
    represents making an attack with a weapon, an Unarmed Strike, or a spell."
    None of the Advantage sources distinguishes the two, so a Blinded caster, a
    Dodging target, and a Paralyzed one have to read identically whether the
    attack came off a sword or out of a spell slot.
    """

    def bolt_caster(self, position: int = 0) -> Creature:
        wren = caster(position=position)
        wren.spells = ("Guiding Bolt",)
        wren.spell_slots = {1: 4}
        wren.spell_attack_bonus = 5
        wren.attacks = (
            AttackOption(
                name="Dagger",
                attack_bonus=5,
                damage=Dice(1, 4, 1),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.MELEE,
                provenance=FIXTURE,
            ),
        )
        return wren

    def mark(self, *, position: int, conditions: Sequence[str] = ()) -> Creature:
        target = Creature(
            name="Mark",
            team="foes",
            ac=15,
            max_hp=200,
            speed=30,
            position=position,
            provenance=FIXTURE,
        )
        for condition in conditions:
            target.add_condition(condition)
        return target

    def bolt(
        self,
        *,
        target_conditions: Sequence[str] = (),
        caster_conditions: Sequence[str] = (),
        distance: int = 5,
        dodging: bool = False,
        rng: Random | None = None,
    ) -> Event:
        """Cast Guiding Bolt at a dummy and return the event describing the attack."""
        driver = Random(4)
        wren = self.bolt_caster()
        for condition in caster_conditions:
            wren.add_condition(condition)
        encounter = Encounter(
            [wren, self.mark(position=distance, conditions=target_conditions)],
            driver,
            spellbook=spellbook(),
        )
        if dodging:
            advance_to(encounter, "Mark", driver)
            encounter.act(Action(kind=ActionKind.DODGE), driver)
        advance_to(encounter, "Wren", driver)
        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Guiding Bolt", targets=("Mark",)),
            Random(9) if rng is None else rng,
        )
        return next(event for event in events if event.kind == "spell_effect")

    @staticmethod
    def rolled_with(event: Event) -> str:
        """The Advantage state the d20 in this event was rolled under.

        Matched on the describe() token rather than by substring, because
        ``"advantage" in "disadvantage"`` is true and would pass either way.
        """
        for state in ("disadvantage", "advantage"):
            if f"] {state} ->" in event.detail:
                return state
        return "none"

    def test_an_unhindered_target_is_attacked_straight(self) -> None:
        assert self.rolled_with(self.bolt()) == "none"

    def test_a_paralyzed_target_grants_advantage(self) -> None:
        event = self.bolt(target_conditions=(Condition.PARALYZED,))
        assert self.rolled_with(event) == "advantage"

    def test_a_restrained_target_grants_advantage(self) -> None:
        event = self.bolt(target_conditions=(Condition.RESTRAINED,))
        assert self.rolled_with(event) == "advantage"

    def test_a_blinded_caster_attacks_with_disadvantage(self) -> None:
        event = self.bolt(caster_conditions=(Condition.BLINDED,))
        assert self.rolled_with(event) == "disadvantage"

    def test_a_frightened_caster_attacks_with_disadvantage(self) -> None:
        event = self.bolt(caster_conditions=(Condition.FRIGHTENED,))
        assert self.rolled_with(event) == "disadvantage"

    def test_a_dodging_target_imposes_disadvantage(self) -> None:
        # SRD 5.2, Dodge: "any attack roll made against you has Disadvantage if
        # you can see the attacker". The _dodging map was never consulted on the
        # cast path, so a Dodge bought nothing against a spell.
        assert self.rolled_with(self.bolt(dodging=True)) == "disadvantage"

    def test_a_blinded_caster_on_a_paralyzed_target_cancels_to_neither(self) -> None:
        event = self.bolt(
            caster_conditions=(Condition.BLINDED,),
            target_conditions=(Condition.PARALYZED,),
        )
        assert self.rolled_with(event) == "none"

    def test_a_hit_on_a_paralyzed_target_within_5_feet_is_a_critical(self) -> None:
        # SRD 5.2, Paralyzed: "Any attack roll that hits you is a Critical Hit if
        # the attacker is within 5 feet of you."
        event = self.bolt(
            target_conditions=(Condition.PARALYZED,), distance=5, rng=FixedRandom(15)
        )
        assert "critical hit" in event.detail

    def test_the_same_hit_from_beyond_5_feet_is_not(self) -> None:
        event = self.bolt(
            target_conditions=(Condition.PARALYZED,), distance=30, rng=FixedRandom(15)
        )
        assert "critical hit" not in event.detail
        assert "-> hit" in event.detail
        # Only the automatic critical is distance-scoped; the Advantage the
        # condition grants applies at any range.
        assert self.rolled_with(event) == "advantage"

    def test_a_prone_target_is_advantaged_within_5_feet_and_disadvantaged_beyond(
        self,
    ) -> None:
        # SRD 5.2, Prone: "An attack roll against you has Advantage if the
        # attacker is within 5 feet of you. Otherwise, that attack roll has
        # Disadvantage." The clause names a distance and no weapon, so a spell
        # attack reads it exactly as a weapon does — nothing here needs to decide
        # what kind of attack a spell is.
        near = self.bolt(target_conditions=(Condition.PRONE,), distance=5)
        far = self.bolt(target_conditions=(Condition.PRONE,), distance=30)
        assert self.rolled_with(near) == "advantage"
        assert self.rolled_with(far) == "disadvantage"

    def test_the_cast_path_and_the_swing_path_agree_about_advantage(self) -> None:
        # The drift guard, and the half of it that still has two code paths to
        # compare: spell_attack_advantage and attack_advantage assemble their
        # arguments separately, and against this target they have to land on the
        # same answer.
        rng = Random(4)
        wren = self.bolt_caster()
        target = self.mark(position=5, conditions=(Condition.PARALYZED,))
        encounter = Encounter([wren, target], rng, spellbook=spellbook())
        dagger = wren.attacks[0]
        assert encounter.spell_attack_advantage(wren, target) == encounter.attack_advantage(
            wren, target, dagger
        )
        assert encounter.spell_attack_advantage(wren, target) is Advantage.ADVANTAGE

    def test_one_forced_critical_rule_serves_both_paths(self) -> None:
        # There is deliberately no spell-specific counterpart to compare against:
        # the rule reads the target's conditions and the attacker's distance and
        # nothing about the attack, so the encounter exposes exactly one method and
        # both paths call it. What is left to pin is the distance scope itself.
        rng = Random(4)
        wren = self.bolt_caster()
        near = self.mark(position=5, conditions=(Condition.PARALYZED,))
        far = self.mark(position=30, conditions=(Condition.PARALYZED,))
        far.name = "Distant"
        encounter = Encounter([wren, near, far], rng, spellbook=spellbook())
        assert encounter.attack_forced_critical(wren, near)
        assert not encounter.attack_forced_critical(wren, far)


class TestTurnLegality:
    def test_an_incapacitated_creature_cannot_act(self) -> None:
        rng = Random(1)
        held = fighter("Held")
        held.add_condition(Condition.PARALYZED)
        encounter = Encounter([held, make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Held", rng)
        with pytest.raises(EncounterError, match="incapacitated"):
            encounter.act(Action(kind=ActionKind.ATTACK, target="Wolf"), rng)

    def test_attacking_after_casting_is_refused(self) -> None:
        # Casting spends the action, and starting an Attack action needs it. Only
        # attacks_left was checked here, so a caster could cast *and* swing on the
        # same turn — worth a fifth of a caster's measured damage per round.
        rng = Random(4)
        wizard = caster(position=0)
        wizard.attacks = (
            AttackOption(
                name="Dagger",
                attack_bonus=5,
                damage=Dice.parse("1d4+2"),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.MELEE,
            ),
        )
        # An Ogre, because a goblin dies to the Fireball and ends the fight before
        # the second action can be refused.
        ogre = make_monster("Ogre", label="Ogre", position=5)
        encounter = Encounter([wizard, ogre], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=25),
            Random(2),
        )
        with pytest.raises(EncounterError, match="already taken an action"):
            encounter.act(
                Action(kind=ActionKind.ATTACK, target="Ogre", attack="Dagger"), rng
            )

    def test_a_multiattack_continues_after_its_first_swing_spends_the_action(
        self,
    ) -> None:
        # The mirror of the above: later swings of a Multiattack must still land,
        # which is why the check is "no attack taken yet" rather than "action used".
        rng = Random(1)
        brute = fighter(attacks_per_action=2)
        encounter = Encounter([brute, make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Wolf"), rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Wolf"), rng)
        assert encounter.state()["turn_state"]["attacks_left"] == 0

    def test_two_actions_in_one_turn_are_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.DODGE), rng)
        with pytest.raises(EncounterError, match="already taken an action"):
            encounter.act(Action(kind=ActionKind.DASH), rng)

    def test_state_reports_the_authoritative_view(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=5)], rng)
        state = encounter.state()
        assert state["round"] == 1
        assert set(state["order"]) == {"Thora", "Wolf"}
        assert {entry["name"] for entry in state["combatants"]} == {"Thora", "Wolf"}
        assert all("hp" in entry and "conditions" in entry for entry in state["combatants"])


class TestConcentrationEffects:
    """A condition a Concentration spell imposes ends when the Concentration does.

    SRD 5.2, Rules Glossary, "Concentration": "Some spells and other effects require
    Concentration to remain active, as specified in their descriptions. If the
    effect's creator loses Concentration, the effect ends." Damage that fails the
    Constitution save, the Incapacitated condition, death and starting a second
    Concentration spell are the four routes named there, and every one of them has
    to reach the creature the spell is holding.

    The awkward case, and the reason this needs a ledger rather than a matching
    ``remove_condition`` next to each ``add_condition``, is two casters holding the
    same creature with the same condition. One losing Concentration must free
    nothing, because the other is still holding it.
    """

    def duel(self, *, targets: int = 1) -> tuple[Encounter, Random, dict[str, Creature]]:
        """A caster, one or two foes to hold, and a brute able to hit the caster."""
        wren = caster(position=0)
        wren.max_hp = wren.hp = 60
        wren.spells = ("Fireball", "Guiding Bolt", "Hold Person")
        wren.spell_slots = {1: 1, 2: 3, 3: 1}
        people: list[Creature] = [wren, fighter("Thora", position=0)]
        for index in range(targets):
            held = fighter(f"Bandit{index}", team="foes", position=10 + index, max_hp=40)
            # Wisdom 6: the save fails on anything but a forced high roll.
            held.abilities[Ability.WISDOM] = 6
            people.append(held)
        people.append(fighter("Brute", team="foes", position=5, max_hp=40))
        rng = Random(11)
        encounter = Encounter(people, rng, spellbook=spellbook())
        return encounter, rng, {c.name: c for c in people}

    def their_turn(self, encounter: Encounter, rng: Random, who: str) -> None:
        """Put ``who`` on a turn with its action still in hand."""
        if (encounter.current_name == who
                and encounter.state()["turn_state"]["action_used"]):
            encounter.advance(rng)
        advance_to(encounter, who, rng)

    def hold(
        self, encounter: Encounter, rng: Random, who: str, target: str,
    ) -> list[Event]:
        """``who`` casts Hold Person on ``target``, forcing the save to fail."""
        self.their_turn(encounter, rng, who)
        return encounter.act(
            Action(kind=ActionKind.CAST, spell="Hold Person", target=target),
            FixedRandom(1),
        )

    # --- the four ways Concentration ends ---------------------------------
    def test_failing_the_concentration_save_frees_the_target(self) -> None:
        encounter, rng, who = self.duel()
        self.hold(encounter, rng, "Wren", "Bandit0")
        assert Condition.PARALYZED in who["Bandit0"].conditions

        advance_to(encounter, "Brute", rng)
        # d20 15 hits AC 13; 1d8 damage; then a natural 1 on the Constitution save.
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Wren", attack="Longsword"),
            ScriptedRandom([15, 5, 1]),
        )
        assert "loses Hold Person" in detail_of(events, "concentration")
        assert who["Wren"].concentrating_on is None
        assert Condition.PARALYZED not in who["Bandit0"].conditions
        assert who["Bandit0"].active

    def test_killing_the_caster_frees_the_target(self) -> None:
        encounter, rng, who = self.duel()
        self.hold(encounter, rng, "Wren", "Bandit0")
        who["Wren"].hp = 4
        advance_to(encounter, "Brute", rng)
        # A hit for more than 4 + max_hp is massive damage: dead outright.
        who["Wren"].max_hp = 4
        encounter.act(
            Action(kind=ActionKind.ATTACK, target="Wren", attack="Longsword"),
            FixedRandom(20),
        )
        assert who["Wren"].dead
        assert Condition.PARALYZED not in who["Bandit0"].conditions
        assert who["Bandit0"].active

    def test_incapacitating_the_caster_frees_the_target(self) -> None:
        """The rival caster holds the caster, and the caster's own hold lapses."""
        encounter, rng, who = self.duel(targets=2)
        rival = who["Bandit1"]
        rival.spells = ("Hold Person",)
        rival.spell_slots = {2: 1}
        rival.spell_save_dc = 15
        self.hold(encounter, rng, "Wren", "Bandit0")
        assert Condition.PARALYZED in who["Bandit0"].conditions

        events = self.hold(encounter, rng, "Bandit1", "Wren")
        assert Condition.PARALYZED in who["Wren"].conditions
        assert who["Wren"].concentrating_on is None
        assert Condition.PARALYZED not in who["Bandit0"].conditions
        assert who["Bandit0"].active
        assert "effect_end" in kinds(events)

    def test_starting_a_second_concentration_spell_ends_the_first(self) -> None:
        """SRD 5.2: Concentration is lost "the moment you start casting" another."""
        encounter, rng, who = self.duel(targets=2)
        self.hold(encounter, rng, "Wren", "Bandit0")
        self.hold(encounter, rng, "Wren", "Bandit1")
        assert who["Wren"].concentrating_on == "Hold Person"
        assert Condition.PARALYZED not in who["Bandit0"].conditions
        assert Condition.PARALYZED in who["Bandit1"].conditions

    def test_a_spell_without_concentration_leaves_the_hold_standing(self) -> None:
        """Only a *Concentration* effect displaces one. Guiding Bolt is not one."""
        encounter, rng, who = self.duel()
        self.hold(encounter, rng, "Wren", "Bandit0")
        self.their_turn(encounter, rng, "Wren")
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Guiding Bolt", target="Brute"),
            Random(3),
        )
        assert who["Wren"].concentrating_on == "Hold Person"
        assert Condition.PARALYZED in who["Bandit0"].conditions

    # --- what must *not* be released --------------------------------------
    def test_a_second_caster_holding_the_same_target_keeps_it_held(self) -> None:
        """Requirement: one caster losing Concentration frees nothing on its own."""
        encounter, rng, who = self.duel(targets=2)
        rival = who["Bandit1"]
        rival.team = "party"
        rival.spells = ("Hold Person",)
        rival.spell_slots = {2: 1}
        rival.spell_save_dc = 15
        self.hold(encounter, rng, "Wren", "Bandit0")
        self.hold(encounter, rng, "Bandit1", "Bandit0")
        assert Condition.PARALYZED in who["Bandit0"].conditions

        # Wren alone is knocked out, which ends only Wren's Concentration.
        who["Wren"].take_damage(who["Wren"].hp)
        encounter.advance(rng)
        assert who["Wren"].concentrating_on is None
        assert who["Bandit1"].concentrating_on == "Hold Person"
        assert Condition.PARALYZED in who["Bandit0"].conditions, (
            "the second caster is still holding this creature"
        )

        # Now the second caster drops it too, and only then is the target free.
        who["Bandit1"].take_damage(who["Bandit1"].hp)
        encounter.advance(rng)
        assert Condition.PARALYZED not in who["Bandit0"].conditions

    def test_a_condition_from_an_untracked_source_survives(self) -> None:
        """A condition the ledger did not grant is not the ledger's to remove.

        The release must be shown to have *happened* — asserting only that the
        condition is still there would pass just as well against an engine that
        never releases anything, which is the defect this class exists for.
        """
        encounter, rng, who = self.duel()
        who["Bandit0"].add_condition(Condition.PARALYZED)
        self.hold(encounter, rng, "Wren", "Bandit0")
        who["Wren"].take_damage(who["Wren"].hp)
        events = encounter.advance(rng)
        assert who["Wren"].concentrating_on is None
        assert "persists" in detail_of(events, "effect_end")
        assert Condition.PARALYZED in who["Bandit0"].conditions

    def test_an_unrelated_condition_is_untouched(self) -> None:
        encounter, rng, who = self.duel()
        who["Bandit0"].add_condition(Condition.POISONED)
        self.hold(encounter, rng, "Wren", "Bandit0")
        who["Wren"].take_damage(who["Wren"].hp)
        events = encounter.advance(rng)
        assert "lifts" in detail_of(events, "effect_end")
        assert Condition.PARALYZED not in who["Bandit0"].conditions
        assert Condition.POISONED in who["Bandit0"].conditions

    def test_an_item_applied_condition_is_not_a_concentration_effect(self) -> None:
        """An item's condition has no Concentration behind it, so nothing ends it."""
        rng = Random(6)
        thrower = fighter("Thora", position=0)
        thrower.items = {"Numbing Dart": 1}
        victim = fighter("Bandit0", team="foes", position=5)
        wren = caster(position=0)
        wren.spell_slots = {2: 1}
        brute = fighter("Brute", team="foes", position=5)
        dart = ItemEffect(condition=Condition.PARALYZED, provenance=FIXTURE)
        encounter = Encounter(
            [thrower, wren, victim, brute], rng,
            spellbook=spellbook(), items={"Numbing Dart": dart},
        )
        advance_to(encounter, "Thora", rng)
        encounter.act(
            Action(kind=ActionKind.USE_ITEM, item="Numbing Dart", target="Bandit0"), rng
        )
        assert Condition.PARALYZED in victim.conditions

        # An unrelated caster now holds the same creature, then drops it. The dart's
        # condition is a different source and must outlive the spell.
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Hold Person", target="Bandit0"),
            FixedRandom(1),
        )
        wren.take_damage(wren.hp)
        events = encounter.advance(rng)
        assert wren.concentrating_on is None
        assert "persists" in detail_of(events, "effect_end")
        assert Condition.PARALYZED in victim.conditions

    # --- the log ----------------------------------------------------------
    def test_the_release_is_reported(self) -> None:
        encounter, rng, who = self.duel()
        self.hold(encounter, rng, "Wren", "Bandit0")
        advance_to(encounter, "Brute", rng)
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Wren", attack="Longsword"),
            ScriptedRandom([15, 5, 1]),
        )
        detail = detail_of(events, "effect_end")
        assert "Hold Person" in detail
        assert str(Condition.PARALYZED) in detail

    def test_the_same_seed_still_produces_the_same_fight(self) -> None:
        """Releases are bookkeeping: they roll nothing and reorder nothing."""
        def transcript() -> list[dict[str, str]]:
            encounter, rng, _ = self.duel(targets=2)
            self.hold(encounter, rng, "Wren", "Bandit0")
            advance_to(encounter, "Brute", rng)
            encounter.act(
                Action(kind=ActionKind.ATTACK, target="Wren", attack="Longsword"),
                ScriptedRandom([15, 5, 1]),
            )
            for _ in range(8):
                if encounter.over:
                    break
                encounter.advance(rng)
            return [event.as_dict() for event in encounter.log]

        first = transcript()
        assert any(event["kind"] == "effect_end" for event in first), (
            "the transcript must contain a release, or it pins nothing"
        )
        assert first == transcript()
