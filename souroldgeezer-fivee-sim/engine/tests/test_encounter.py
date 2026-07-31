"""Encounter tests: initiative, turns, damage, reactions, spell resources.

Because the generator is passed to each call rather than held by the encounter,
these tests build a fight with an ordinary seed and then resolve a specific action
with a forced generator. That is how a single attack's outcome gets pinned without
contriving the whole fight.
"""

from __future__ import annotations

from collections.abc import Sequence
from random import Random
from types import MappingProxyType
from typing import Any

import pytest

from fivee_sim.content import make_monster, spellbook
from fivee_sim.kernel.actions import AttackKind
from fivee_sim.kernel.conditions import Condition
from fivee_sim.kernel.dice import Advantage, Dice
from fivee_sim.kernel.grid import (
    CoverGrade,
    DiagonalRule,
    Point,
    Square,
    as_point,
    square_center,
    to_square,
)
from fivee_sim.kernel.items import ItemEffect
from fivee_sim.kernel.rules import Ability, DamageType
from fivee_sim.kernel.spells import Spell
from fivee_sim.model.battlemap import (
    BattleMap,
    FeatureCheck,
    FeatureOverlay,
    HeightPair,
    MapFeature,
    MapPlane,
    TerrainPair,
)
from fivee_sim.model.creature import AttackOption, Creature
from fivee_sim.model.encounter import (
    Action,
    ActionKind,
    Encounter,
    EncounterError,
    Event,
)

from .conftest import (
    FIXTURE,
    FixedRandom,
    ScriptedRandom,
    advance_to,
    caster,
    fighter,
    shaped_spellbook,
    shaper,
)


def kinds(events: Sequence[Event]) -> list[str]:
    return [event.kind for event in events]


def detail_of(events: Sequence[Event], kind: str) -> str:
    """The detail of the one event of ``kind``, asserting there is exactly one."""
    matching = [event for event in events if event.kind == kind]
    assert len(matching) == 1, f"expected one {kind!r} event, got {len(matching)}"
    return matching[0].detail


def rolled_with(event: Event) -> str:
    """The Advantage state the d20 in this event was rolled under.

    Matched on the ``describe()`` token rather than by substring, because
    ``"advantage" in "disadvantage"`` is true and a substring test would pass
    whichever way the roll actually went. Every assertion about the state a d20
    was rolled under goes through here — attack rolls and saving throws alike,
    since both render the same ``[faces] <state> ->`` shape.
    """
    for state in ("disadvantage", "advantage"):
        if f"] {state} ->" in event.detail:
            return state
    return "none"


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

    def test_a_natural_20_revival_leaves_the_full_movement_budget(self) -> None:
        """SRD 5.2, "Death Saving Throws", Rolling 20: "If you roll a 20 on the
        d20, you regain 1 Hit Point." The save is rolled at the start of the
        creature's own turn, so the revived creature is conscious for the rest of
        it — and a conscious creature may move up to its Speed on its turn.
        Deriving the budget before the save froze ``movement_left`` at 0 while
        the attack budget was granted regardless.
        """
        encounter, hero = self._dying_hero()
        # A forced 20 is the natural 20: regain 1 hit point and wake.
        advance_to(encounter, "Hero", FixedRandom(20))
        assert hero.conscious
        assert hero.hp == 1
        # Revived, not tidied up: still Prone, and standing costs half Speed.
        assert Condition.PRONE in hero.conditions
        assert encounter.state()["turn_state"]["movement_left"] == hero.speed

    def test_a_still_dying_creature_has_no_movement_budget(self) -> None:
        encounter, hero = self._dying_hero()
        # A forced 15 succeeds without reviving: one success, still down.
        advance_to(encounter, "Hero", FixedRandom(15))
        assert not hero.conscious
        assert hero.dying
        assert encounter.state()["turn_state"]["movement_left"] == 0

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

    def test_passing_straight_through_reach_provokes_without_a_map(self) -> None:
        # The endpoint check never caught this: start and end both out of the
        # goblin's reach, with the straight walk crossing it on the way.
        rng = Random(6)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=10)
        encounter = Encounter([fighter(), goblin], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        assert kinds(events).count("opportunity_attack") == 1

        # The reaction is spent, observed through the public surface rather than
        # through _reaction_available: Dash buys enough movement to walk back
        # across the goblin's reach in the same round, and that second provoking
        # pass draws nothing. The goblin's reaction only refreshes when its own
        # turn begins, which has not happened yet.
        encounter.act(Action(kind=ActionKind.DASH), rng)
        again = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=0), FixedRandom(20)
        )
        assert encounter.creatures["Thora"].position == (0, 0)
        assert "opportunity_attack" not in kinds(again)

    def test_a_disengaged_pass_through_does_not_provoke_without_a_map(self) -> None:
        rng = Random(6)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=10)
        encounter = Encounter([fighter(), goblin], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.DISENGAGE), rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        assert "opportunity_attack" not in kinds(events)

    def test_a_mover_dropped_by_the_attack_stops_at_the_leave_point(self) -> None:
        # The move event still declares the full 30 ft, but the state is the
        # truth: Thora falls at (20, 0), the first sample beyond the goblin's
        # reach, not at the destination she never got to.
        rng = Random(6)
        thora = fighter(hp=1)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=10)
        encounter = Encounter([thora, goblin], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        assert "opportunity_attack" in kinds(events)
        assert not thora.conscious
        assert as_point(thora.position) == (20, 0)

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
        assert encounter.creatures["Thora"].position == (60, 0)

    def test_a_grappled_creature_cannot_move(self) -> None:
        rng = Random(1)
        held = fighter("Held", position=0)
        held.add_condition(Condition.GRAPPLED)
        encounter = Encounter([held, make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Held", rng)
        with pytest.raises(EncounterError, match="speed 0"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=20), rng)


class TestPlanarMovement:
    """Movement on the plane: two-dimensional destinations, diagonal rules."""

    def test_a_diagonal_move_costs_the_longer_axis_under_the_default_rule(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=90)], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=(20, 15)), rng)
        move = next(event for event in events if event.kind == "move")
        assert move.data["origin"] == (0, 0)
        assert move.data["destination"] == (20, 15)
        assert move.data["cost"] == 20
        assert encounter.creatures["Thora"].position == (20, 15)
        assert encounter.state()["turn_state"]["movement_left"] == 10

    def test_the_5_10_5_rule_charges_every_second_diagonal_double(self) -> None:
        from fivee_sim.kernel.grid import DiagonalRule

        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=90)], rng,
            movement_rule=DiagonalRule.FIVE_TEN_FIVE,
        )
        advance_to(encounter, "Thora", rng)
        # (25, 25) is 25 + 12 = 37 ft under 5-10-5, past a 30 ft speed.
        with pytest.raises(EncounterError, match="movement"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(25, 25)), rng)
        # (20, 20) is 20 + 10 = 30 ft: exactly the speed.
        encounter.act(Action(kind=ActionKind.MOVE, to_position=(20, 20)), rng)
        assert encounter.state()["turn_state"]["movement_left"] == 0

    def test_state_reports_positions_as_x_y_pairs(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(position=(10, 20)), make_monster("Wolf", position=5)], rng
        )
        positions = {
            entry["name"]: entry["position"]
            for entry in encounter.state()["combatants"]
        }
        assert positions == {"Thora": [10, 20], "Wolf": [5, 0]}

    def test_waypoints_are_refused_without_a_battle_map(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=90)], rng)
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="battle map"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(10, 0), path=((5, 0),)), rng
            )


def archer(name: str = "Sylvi", *, position: int | tuple[int, int] = 0,
           team: str = "party") -> Creature:
    return Creature(
        name=name,
        team=team,
        ac=14,
        max_hp=20,
        attacks=(
            AttackOption(
                name="Shortbow",
                attack_bonus=5,
                damage=Dice(1, 6, 3),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.RANGED,
                normal_range=80,
                long_range=320,
                provenance=FIXTURE,
            ),
        ),
        position=position,
        provenance=FIXTURE,
    )


def strip(
    width: int,
    height: int = 1,
    *,
    terrain: dict[Square, str] | None = None,
    elevation: dict[Square, int] | None = None,
    features: tuple[MapFeature, ...] = (),
) -> BattleMap:
    return BattleMap.flat(
        name="test map",
        width=width,
        height=height,
        terrain=terrain or {},
        elevation=elevation or {},
        features={feature.name: feature for feature in features},
        provenance=FIXTURE,
    )


def tower(
    *,
    stair_at: Square = (1, 0),
    upper_feet: int = 10,
    ground_terrain: dict[Square, str] | None = None,
    upper_terrain: dict[Square, str] | None = None,
) -> BattleMap:
    """Two 4x1 floors over one footprint, joined by a stair, the upper one raised."""
    return BattleMap(
        name="tower",
        width=4,
        height=1,
        levels=MappingProxyType(
            {
                0: MapPlane(
                    default_terrain="floor",
                    terrain=ground_terrain or {},
                    connectors={stair_at: 1},
                ),
                1: MapPlane(
                    default_terrain="floor",
                    terrain=upper_terrain or {},
                    default_elevation=upper_feet,
                    connectors={stair_at: 0},
                ),
            }
        ),
        provenance=FIXTURE,
    )


class TestLevels:
    def test_a_creature_stands_on_the_ground_unless_it_says_otherwise(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng, battle_map=tower()
        )
        assert [c["level"] for c in encounter.state()["combatants"]] == [0, 0]

    def test_two_creatures_may_hold_one_square_on_different_levels(self) -> None:
        rng = Random(1)
        upstairs = make_monster("Wolf", position=(0, 0))
        upstairs.level = 1
        encounter = Encounter([fighter(position=(0, 0)), upstairs], rng, battle_map=tower())
        # Both resolve to the same square; only the level tells them apart. On
        # one plane this pair is refused ("both start in square").
        thora, wolf = encounter.creatures["Thora"], encounter.creatures["Wolf"]
        assert to_square(as_point(thora.position)) == to_square(as_point(wolf.position))
        assert (thora.level, wolf.level) == (0, 1)

    def test_a_floor_is_total_cover(self) -> None:
        rng = Random(1)
        upstairs = make_monster("Wolf", position=(0, 0))
        upstairs.level = 1
        encounter = Encounter([fighter(position=(0, 0)), upstairs], rng, battle_map=tower())
        assert encounter.cover_between("Thora", "Wolf") is CoverGrade.TOTAL

    def test_an_attack_through_a_floor_finds_no_line(self) -> None:
        rng = Random(1)
        upstairs = make_monster("Wolf", position=(0, 0))
        upstairs.level = 1
        encounter = Encounter([fighter(position=(0, 0)), upstairs], rng, battle_map=tower())
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Wolf"), rng
        )
        attack = next(event for event in events if event.kind == "attack")
        assert attack.data["total_cover"] is True

    def test_an_enemy_upstairs_is_not_an_enemy_within_reach(self) -> None:
        rng = Random(1)
        upstairs = make_monster("Wolf", position=(0, 0))
        upstairs.level = 1
        encounter = Encounter([fighter(position=(0, 0)), upstairs], rng, battle_map=tower())
        assert encounter.enemies_of("Thora") == []

    def test_a_connector_carries_a_mover_up(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng, battle_map=tower()
        )
        advance_to(encounter, "Thora", rng)
        encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(5, 0), to_level=1), rng
        )
        thora = encounter.creatures["Thora"]
        assert thora.level == 1
        assert to_square(as_point(thora.position)) == (1, 0)

    def test_climbing_a_storey_costs_the_climb(self) -> None:
        # 5 ft to walk to the stair, then a 10-foot rise: over CLIMB_FEET, so
        # 5 ft of horizontal plus 2 ft per foot climbed = 25. Exactly a speed.
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng, battle_map=tower()
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(5, 0), to_level=1), rng
        )
        move = next(event for event in events if event.kind == "move")
        assert move.data["cost"] == 30
        assert move.data["to_level"] == 1
        assert encounter.state()["turn_state"]["movement_left"] == 0

    def test_a_storey_too_high_to_climb_is_refused(self) -> None:
        # 5 ft to the stair plus a 40-foot rise at 2 ft per foot climbed, on top
        # of the 5-foot step in: 5 + 5 + 80 = 90, three times a fighter's speed.
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng,
            battle_map=tower(upper_feet=40),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="needs 90 ft"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(5, 0), to_level=1), rng)

    def test_a_move_to_a_level_needs_a_connector_on_the_square(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng, battle_map=tower()
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="nothing at .* leads to level 1"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 0), to_level=1), rng)

    def test_a_move_to_a_level_the_map_does_not_have_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng, battle_map=tower()
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="no level 7"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(5, 0), to_level=7), rng)

    def test_a_wall_upstairs_does_not_block_the_ground(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng,
            battle_map=tower(upper_terrain={(2, 0): "wall"}),
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 0)), rng)
        move = next(event for event in events if event.kind == "move")
        assert move.data["cost"] == 10

    def test_a_creature_upstairs_reports_the_storeys_height(self) -> None:
        rng = Random(1)
        upstairs = make_monster("Wolf", position=(0, 0))
        upstairs.level = 1
        encounter = Encounter([fighter(position=(0, 0)), upstairs], rng, battle_map=tower())
        wolf = next(c for c in encounter.state()["combatants"] if c["name"] == "Wolf")
        assert (wolf["level"], wolf["elevation"]) == (1, 10)

    def test_a_connector_arriving_in_a_wall_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng,
            battle_map=tower(upper_terrain={(1, 0): "wall"}),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="arrives on impassable 'wall'"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(5, 0), to_level=1), rng)

    def test_a_connector_arriving_on_an_occupied_square_is_refused(self) -> None:
        rng = Random(1)
        upstairs = make_monster("Wolf", position=(5, 0))
        upstairs.level = 1
        encounter = Encounter([fighter(), upstairs], rng, battle_map=tower())
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="on level 1 is occupied by Wolf"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(5, 0), to_level=1), rng)

    def test_a_connector_to_a_level_the_map_lacks_is_refused_at_adoption(self) -> None:
        # A hand-built battle map can carry one; the document parser refuses it
        # earlier, but the map need not have come from a document.
        rng = Random(1)
        broken = BattleMap(
            name="broken tower", width=4, height=1,
            levels=MappingProxyType({
                0: MapPlane(default_terrain="floor", connectors={(1, 0): 3}),
            }),
            provenance=FIXTURE,
        )
        with pytest.raises(EncounterError, match="leads to level 3, which this map does not have"):
            Encounter([fighter(), make_monster("Wolf", position=(15, 0))], rng,
                      battle_map=broken)

    def test_a_combatant_placed_on_a_level_the_map_lacks_is_refused(self) -> None:
        rng = Random(1)
        stray = make_monster("Wolf", position=(15, 0))
        stray.level = 3
        with pytest.raises(EncounterError, match="level 3"):
            Encounter([fighter(), stray], rng, battle_map=tower())

    def test_the_map_summary_lists_every_level(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng, battle_map=tower()
        )
        levels = encounter.state()["map"]["levels"]
        assert [level["index"] for level in levels] == [0, 1]
        assert levels[1]["elevation"]["default"] == 10


class TestMapMovement:
    def test_difficult_terrain_charges_double_for_every_entered_square(self) -> None:
        rng = Random(1)
        battle_map = strip(6, terrain={(2, 0): "difficult", (3, 0): "difficult"})
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(25, 0))], rng,
            battle_map=battle_map,
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=(20, 0)), rng)
        move = next(event for event in events if event.kind == "move")
        assert move.data["cost"] == 30  # 5 + 10 + 10 + 5
        assert move.data["squares"] == [[0, 0], [1, 0], [2, 0], [3, 0], [4, 0]]
        assert encounter.state()["turn_state"]["movement_left"] == 0

    def test_a_move_the_terrain_makes_unaffordable_is_refused(self) -> None:
        rng = Random(1)
        battle_map = strip(7, terrain={(2, 0): "difficult", (3, 0): "difficult"})
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(30, 0))], rng,
            battle_map=battle_map,
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="needs 35 ft"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(25, 0)), rng)

    def test_a_wall_forces_the_route_around_it(self) -> None:
        rng = Random(1)
        battle_map = strip(4, 3, terrain={(1, 0): "wall", (1, 1): "wall"})
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 10))], rng,
            battle_map=battle_map,
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 0)), rng)
        move = next(event for event in events if event.kind == "move")
        assert move.data["cost"] == 20
        walked = {tuple(square) for square in move.data["squares"]}
        assert not walked & {(1, 0), (1, 1)}

    def test_a_walled_off_destination_is_refused(self) -> None:
        rng = Random(1)
        battle_map = strip(4, terrain={(1, 0): "wall"})
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng,
            battle_map=battle_map,
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="no route"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 0)), rng)

    def test_a_move_may_not_end_on_a_conscious_creature(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(10, 0))], rng,
            battle_map=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="occupied by Wolf"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 0)), rng)

    def test_a_move_off_the_map_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(10, 0))], rng,
            battle_map=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="off the 5x1 map"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(40, 0)), rng)

    def test_allies_can_be_crossed_but_not_stopped_on(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [
                fighter(),
                fighter("Ally", position=(10, 0)),
                make_monster("Wolf", position=(20, 0)),
            ],
            rng,
            battle_map=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=(15, 0)), rng)
        move = next(event for event in events if event.kind == "move")
        assert [2, 0] in move.data["squares"]  # straight through the ally

    def test_an_enemy_blocks_the_only_route(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [
                fighter(),
                make_monster("Goblin Warrior", label="Goblin", position=(10, 0)),
                make_monster("Wolf", position=(20, 0)),
            ],
            rng,
            battle_map=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="no route"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(15, 0)), rng)

    def test_passing_through_reach_provokes_even_when_the_move_ends_clear(self) -> None:
        # The 1-D endpoint check never caught this: start and end both out of
        # reach, with the walk crossing the goblin's threat on the way.
        rng = Random(6)
        battle_map = strip(5, 2)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=(10, 5))
        encounter = Encounter([fighter(), goblin], rng, battle_map=battle_map)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(20, 0)), FixedRandom(20)
        )
        assert "opportunity_attack" in kinds(events)

    def test_disengage_suppresses_the_pass_through_attack(self) -> None:
        rng = Random(6)
        battle_map = strip(5, 2)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=(10, 5))
        encounter = Encounter([fighter(), goblin], rng, battle_map=battle_map)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.DISENGAGE), rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(20, 0)), FixedRandom(20)
        )
        assert "opportunity_attack" not in kinds(events)

    def test_an_explicit_path_is_honoured_when_legal(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(20, 0))], rng,
            battle_map=strip(5, 2),
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(
                kind=ActionKind.MOVE,
                to_position=(10, 0),
                path=((5, 5), (10, 5), (10, 0)),
            ),
            rng,
        )
        move = next(event for event in events if event.kind == "move")
        assert move.data["squares"] == [[0, 0], [1, 1], [2, 1], [2, 0]]
        assert move.data["cost"] == 15

    def test_a_path_with_a_gap_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(20, 0))], rng,
            battle_map=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="not to an adjacent square"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(10, 0), path=((10, 0),)),
                rng,
            )

    def test_a_path_through_a_wall_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng,
            battle_map=strip(4, terrain={(1, 0): "wall"}),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="impassable 'wall'"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(10, 0),
                       path=((5, 0), (10, 0))),
                rng,
            )

    def test_a_path_must_end_at_the_destination(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(20, 0))], rng,
            battle_map=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="ends at"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(10, 0), path=((5, 0),)),
                rng,
            )


class TestMapElevation:
    """Ground height on a fight's map: slopes, climbs, and what it does not touch."""

    def fight(self, battle_map: BattleMap, rng: Random, wolf: Point = (25, 10)) -> Encounter:
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=wolf)], rng, battle_map=battle_map
        )
        advance_to(encounter, "Thora", rng)
        return encounter

    def moving(
        self,
        battle_map: BattleMap,
        to: Point,
        rng: Random,
        wolf: Point = (25, 10),
        **kwargs: Any,
    ) -> dict[str, Any]:
        encounter = self.fight(battle_map, rng, wolf=wolf)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=to, **kwargs), rng)
        return next(event for event in events if event.kind == "move").data

    def test_a_slope_is_difficult_terrain(self) -> None:
        # A one-row corridor, so the route cannot decline the grade.
        rng = Random(1)
        battle_map = strip(6, elevation={(2, 0): 5, (3, 0): 10, (4, 0): 10})
        move = self.moving(battle_map, (20, 0), rng, wolf=(25, 0))
        assert move["cost"] == 5 + 10 + 10 + 5  # only the two rises cost double

    def test_a_slope_through_rough_going_is_not_doubled_twice(self) -> None:
        # SRD 5.2: Difficult Terrain "isn't cumulative" — a slope over
        # undergrowth is the same 10 feet a slope over grass is, not 20.
        rng = Random(1)
        battle_map = strip(
            6,
            terrain={(2, 0): "difficult"},
            elevation={(2, 0): 5, (3, 0): 5, (4, 0): 5},
        )
        move = self.moving(battle_map, (20, 0), rng, wolf=(25, 0))
        assert move["cost"] == 5 + 10 + 5 + 5

    def test_a_cliff_costs_the_climb(self) -> None:
        rng = Random(1)
        # A 10-foot face: the step into it costs the square plus a foot for each
        # foot climbed, which is most of a 30-foot Speed for one square.
        move = self.moving(strip(6, 3, elevation={(1, 0): 10}), (5, 0), rng)
        assert move["cost"] == 5 + 20
        assert move["squares"] == [[0, 0], [1, 0]]

    def test_climbing_down_costs_what_climbing_up_costs(self) -> None:
        rng = Random(1)
        # Thora starts at (0, 0), which this map puts on a 10-foot ledge.
        move = self.moving(strip(6, 3, elevation={(0, 0): 10}), (5, 0), rng)
        assert move["cost"] == 5 + 20

    def test_a_route_walks_round_a_cliff_to_reach_its_top(self) -> None:
        rng = Random(1)
        # A 20-foot plateau along the top row, walled off head-on but reachable
        # up a ramp that rises five feet a square through row 1.
        battle_map = strip(
            6, 2,
            elevation={
                (2, 0): 20, (3, 0): 20, (4, 0): 20, (5, 0): 20,
                (2, 1): 5, (3, 1): 10, (4, 1): 15, (5, 1): 20,
            },
        )
        route = self.fight(battle_map, rng, wolf=(0, 5)).route("Thora", (5, 0))
        assert route is not None
        walked = set(route.squares)
        assert not walked & {(2, 0), (3, 0)}  # never up the face
        assert route.cost_feet == 5 + 10 * 4  # one level step, then four slopes

    def test_a_climb_beyond_the_budget_is_refused(self) -> None:
        rng = Random(1)
        encounter = self.fight(strip(6, 3, elevation={(1, 0): 60}), rng)
        with pytest.raises(EncounterError, match="needs 125 ft"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(5, 0)), rng)

    def test_an_explicit_path_is_charged_the_same_climb(self) -> None:
        rng = Random(1)
        move = self.moving(
            strip(6, 3, elevation={(2, 0): 10}), (10, 0), rng,
            path=((5, 0), (10, 0)),
        )
        assert move["cost"] == 5 + (5 + 20)

    def test_sight_and_cover_are_measured_flat(self) -> None:
        # The limit this version keeps: a ridge between two creatures screens
        # neither of them.
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(20, 0))], rng,
            battle_map=strip(6, elevation={(2, 0): 40}),
        )
        assert encounter.cover_between("Thora", "Wolf") is CoverGrade.NONE

    def test_state_reports_the_ground_underfoot(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(20, 0))], rng,
            battle_map=strip(6, elevation={(4, 0): 25}),
        )
        state = encounter.state()
        heights = {c["name"]: c["elevation"] for c in state["combatants"]}
        assert heights == {"Thora": 0, "Wolf": 25}
        assert state["map"]["elevation"]["flat"] is False
        assert (state["map"]["elevation"]["min"], state["map"]["elevation"]["max"]) == (0, 25)

    def test_a_fight_without_a_map_reports_no_ground_at_all(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=(20, 0))], rng)
        assert all("elevation" not in c for c in encounter.state()["combatants"])


class TestMapPlacement:
    def test_starting_inside_a_wall_is_refused(self) -> None:
        with pytest.raises(EncounterError, match="impassable 'wall'"):
            Encounter(
                [fighter(position=(5, 0)), make_monster("Wolf", position=(15, 0))],
                Random(1),
                battle_map=strip(4, terrain={(1, 0): "wall"}),
            )

    def test_starting_off_the_map_is_refused(self) -> None:
        with pytest.raises(EncounterError, match="off the 4x1 map"):
            Encounter(
                [fighter(position=(25, 0)), make_monster("Wolf", position=(15, 0))],
                Random(1),
                battle_map=strip(4),
            )

    def test_two_combatants_may_not_share_a_square(self) -> None:
        with pytest.raises(EncounterError, match="both start in square"):
            Encounter(
                [fighter(position=(0, 0)), make_monster("Wolf", position=(2, 2))],
                Random(1),
                battle_map=strip(4),
            )

    def test_positions_snap_to_the_centre_of_their_square(self) -> None:
        encounter = Encounter(
            [fighter(position=(7, 3)), make_monster("Wolf", position=(15, 0))],
            Random(1),
            battle_map=strip(4),
        )
        assert encounter.creatures["Thora"].position == (5, 0)

    def test_a_map_naming_unknown_terrain_is_refused_with_the_loaded_kinds(
        self,
    ) -> None:
        with pytest.raises(EncounterError, match="vale-lava"):
            Encounter(
                [fighter(), make_monster("Wolf", position=(15, 0))],
                Random(1),
                battle_map=strip(4, terrain={(2, 0): "vale-lava"}),
            )

    def test_the_map_block_appears_in_state(self) -> None:
        door = MapFeature(name="crypt door", square=(1, 0))
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))],
            Random(1),
            battle_map=strip(4, features=(door,)),
        )
        block = encounter.state()["map"]
        assert block == {
            "name": "test map",
            "width": 4,
            "height": 1,
            "movement_rule": "5-5-5",
            "elevation": {
                "default": 0,
                "min": 0,
                "max": 0,
                "flat": True,
                "affects": "movement only; sight, cover, and areas are measured flat",
            },
            "levels": [
                {
                    "index": 0,
                    "elevation": {
                        "default": 0,
                        "min": 0,
                        "max": 0,
                        "flat": True,
                        "affects": (
                            "movement only; sight, cover, and areas are measured flat"
                        ),
                    },
                    "connectors": [],
                },
            ],
            "features": {
                "crypt door": {
                    "square": [1, 0], "kind": "door", "level": 0, "open": False,
                },
            },
        }

    def test_a_mapless_fight_reports_no_map(self) -> None:
        encounter = Encounter([fighter(), make_monster("Wolf", position=5)], Random(1))
        assert encounter.state()["map"] is None


class TestCoverChangesTheAttack:
    #: A full wall column with no gap: the only geometry that seals sight, since
    #: the corner rule sees past a lone pillar.
    WALL_COLUMN = {(2, 0): "wall", (2, 1): "wall", (2, 2): "wall"}

    def duel(self, terrain: dict[Square, str]) -> Encounter:
        rng = Random(3)
        encounter = Encounter(
            [archer(position=(0, 5)),
             make_monster("Goblin Warrior", label="Goblin", position=(20, 5))],
            rng,
            battle_map=strip(5, 3, terrain=terrain),
        )
        advance_to(encounter, "Sylvi", rng)
        return encounter

    def test_half_cover_turns_a_pinned_hit_into_a_miss(self) -> None:
        # Natural 11 + 5 = 16: a hit against AC 15 in the open, a miss against
        # 15 + 2 behind the half-cover pillar. Same roll, different fight.
        open_ground = self.duel({})
        events = open_ground.act(
            Action(kind=ActionKind.ATTACK, target="Goblin"), FixedRandom(11)
        )
        assert events[0].data["hit"]
        assert events[0].data["cover"] == 0

        covered = self.duel({(2, 1): "half-cover"})
        events = covered.act(
            Action(kind=ActionKind.ATTACK, target="Goblin"), FixedRandom(11)
        )
        assert not events[0].data["hit"]
        assert events[0].data["cover"] == 1
        assert "half cover, +2 AC" in events[0].detail

    def test_total_cover_refuses_without_consuming_the_attack(self) -> None:
        sealed = self.duel(self.WALL_COLUMN)
        before = sealed.state()["turn_state"]
        events = sealed.act(
            Action(kind=ActionKind.ATTACK, target="Goblin"), FixedRandom(20)
        )
        assert events[0].data["total_cover"]
        assert "total cover" in events[0].detail
        goblin = sealed.creatures["Goblin"]
        assert goblin.hp == goblin.max_hp
        assert sealed.state()["turn_state"] == before

    def test_cover_between_is_the_public_authority(self) -> None:
        assert self.duel({}).cover_between("Sylvi", "Goblin") is CoverGrade.NONE
        assert self.duel({(2, 1): "half-cover"}).cover_between(
            "Sylvi", "Goblin"
        ) is CoverGrade.HALF
        assert self.duel(self.WALL_COLUMN).cover_between(
            "Sylvi", "Goblin"
        ) is CoverGrade.TOTAL

    def test_an_intervening_creature_grants_half_cover(self) -> None:
        rng = Random(3)
        encounter = Encounter(
            [
                archer(position=(0, 5)),
                fighter("Ally", position=(10, 5)),
                make_monster("Goblin Warrior", label="Goblin", position=(20, 5)),
            ],
            rng,
            battle_map=strip(5, 3),
        )
        assert encounter.cover_between("Sylvi", "Goblin") is CoverGrade.HALF


class TestCoverShieldsSaves:
    """Cover on Dexterity saves against areas, measured from the effect's origin."""

    def goblin_effect(self, events: Sequence[Event]) -> Event:
        return next(
            event for event in events
            if event.kind == "spell_effect" and event.target == "Goblin"
        )

    def fireball_at_own_feet(self, terrain: dict[Square, str]) -> Sequence[Event]:
        """Wren drops a Fireball on her own square; the goblin sits 20 ft out,
        with whatever ``terrain`` puts between the origin and it."""
        rng = Random(3)
        encounter = Encounter(
            [caster(position=(0, 5)),
             make_monster("Goblin Warrior", label="Goblin", position=(20, 5))],
            rng,
            spellbook=spellbook(),
            battle_map=strip(5, 3, terrain=terrain),
        )
        advance_to(encounter, "Wren", rng)
        return encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3,
                   center=(0, 5)),
            FixedRandom(12),
        )

    def test_half_cover_flips_a_pinned_dexterity_save(self) -> None:
        # Natural 12 + 2 (Dex) = 14: a failure against DC 15 in the open. Behind
        # the half-cover pillar the same roll gains +2 and saves at 16.
        in_the_open = self.goblin_effect(self.fireball_at_own_feet({}))
        assert in_the_open.data["saved"] is False
        assert "cover" not in in_the_open.data

        behind_cover = self.goblin_effect(
            self.fireball_at_own_feet({(2, 1): "half-cover"})
        )
        assert behind_cover.data["saved"] is True
        assert behind_cover.data["cover"] == 1

    def test_a_non_dexterity_save_gets_no_cover_bonus(self) -> None:
        # Shatter saves on Constitution: the goblin's half cover is reported in
        # the payload but grants nothing. Natural 13 + 0 (Con) = 13 fails DC 15;
        # were the +2 wrongly applied, 15 would save.
        rng = Random(3)
        wizard = caster(position=(0, 5))
        wizard.spells = ("Shatter",)
        encounter = Encounter(
            [wizard, make_monster("Goblin Warrior", label="Goblin",
                                  position=(20, 5))],
            rng,
            spellbook=spellbook(),
            battle_map=strip(5, 3, terrain={(3, 1): "half-cover"}),
        )
        advance_to(encounter, "Wren", rng)
        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Shatter", slot_level=2,
                   center=(10, 5)),
            FixedRandom(13),
        )
        effect = self.goblin_effect(events)
        assert effect.data["cover"] == 1
        assert effect.data["saved"] is False

    def test_total_cover_from_the_origin_excludes_the_target(self) -> None:
        # The sealed goblin is inside the template — 15 ft from the origin — but
        # a full-height wall stands between; the blast does not reach around it.
        rng = Random(3)
        encounter = Encounter(
            [
                caster(position=(0, 5)),
                make_monster("Goblin Warrior", label="Near", position=(10, 5)),
                make_monster("Goblin Warrior", label="Sealed", position=(30, 5)),
            ],
            rng,
            spellbook=spellbook(),
            battle_map=strip(
                8, 3, terrain={(4, 0): "wall", (4, 1): "wall", (4, 2): "wall"}
            ),
        )
        advance_to(encounter, "Wren", rng)
        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3,
                   center=(15, 5)),
            Random(2),
        )
        struck = {e.target for e in events if e.kind == "spell_effect"}
        assert "Near" in struck
        assert "Sealed" not in struck
        sealed = encounter.creatures["Sealed"]
        assert sealed.hp == sealed.max_hp


class TestCoverReachesNamedTargetSpells:
    """Cover on a spell aimed at a named creature, measured from the caster.

    SRD 5.2, "Cover" (p. 179): Half Cover is "+2 bonus to AC and Dexterity saving
    throws", Three-Quarters Cover "+5", and Total Cover "can't be targeted
    directly" — *directly* being the word that separates this from an area, which
    reaches whoever its template catches. The rule names no attack/spell split, so
    a spell aimed at a creature is shielded exactly as an arrow is.

    ``TestCoverShieldsSaves`` covers the area branch; this is the named one, which
    used to consult cover nowhere at all.
    """

    WALL_COLUMN = TestCoverChangesTheAttack.WALL_COLUMN

    def duel(self, terrain: dict[Square, str], *, spell: str = "Guiding Bolt",
             book: dict[str, Spell] | None = None) -> Encounter:
        rng = Random(3)
        wren = caster(position=(0, 5))
        wren.spells = (spell,)
        wren.spell_slots = {1: 1}
        encounter = Encounter(
            [wren, make_monster("Goblin Warrior", label="Goblin", position=(20, 5))],
            rng,
            spellbook=spellbook() if book is None else book,
            battle_map=strip(5, 3, terrain=terrain),
        )
        advance_to(encounter, "Wren", rng)
        return encounter

    def test_total_cover_refuses_a_named_target_spell(self) -> None:
        sealed = self.duel(self.WALL_COLUMN)
        before = sealed.state()["turn_state"]
        with pytest.raises(EncounterError, match="total cover"):
            sealed.act(
                Action(kind=ActionKind.CAST, spell="Guiding Bolt", slot_level=1,
                       target="Goblin"),
                FixedRandom(20),
            )
        goblin = sealed.creatures["Goblin"]
        assert goblin.hp == goblin.max_hp
        # Refused before anything is spent, exactly as an out-of-range cast is.
        assert sealed.creatures["Wren"].spell_slots[1] == 1
        assert sealed.state()["turn_state"] == before

    def test_half_cover_raises_ac_against_a_spell_attack_roll(self) -> None:
        # Natural 9 + 6 = 15: a hit against the goblin's AC 15 in the open, a
        # miss against 15 + 2 behind the pillar. Same roll, same seed.
        in_the_open = self.duel({}).act(
            Action(kind=ActionKind.CAST, spell="Guiding Bolt", slot_level=1,
                   target="Goblin"),
            FixedRandom(9),
        )
        struck = next(e for e in in_the_open if e.kind == "spell_effect")
        assert struck.data["affected"] and struck.data["damage"]
        assert "vs AC 15 -> hit" in struck.detail

        behind_cover = self.duel({(2, 1): "half-cover"}).act(
            Action(kind=ActionKind.CAST, spell="Guiding Bolt", slot_level=1,
                   target="Goblin"),
            FixedRandom(9),
        )
        shielded = next(e for e in behind_cover if e.kind == "spell_effect")
        assert not shielded.data["affected"] and shielded.data["damage"] == 0
        assert shielded.data["cover"] == 1
        # The raised AC is in the log, not just the outcome: a bare "miss" would
        # pass against a build that rolled worse rather than one that applied +2.
        assert "vs AC 17 -> miss" in shielded.detail

    def test_half_cover_shields_a_named_dexterity_save(self) -> None:
        # No bundled spell aims a Dexterity save at a named creature, so this
        # needs a fixture. Natural 12 + 2 (Dex) = 14 fails DC 15; the +2 for half
        # cover makes the same roll a 16 and a save.
        ray = Spell(
            name="Searing Ray",
            level=1,
            save_ability=Ability.DEXTERITY,
            damage=Dice(3, 6, 0),
            damage_type=DamageType.FIRE,
            range_feet=120,
            provenance=FIXTURE,
        )
        book = {"Searing Ray": ray}
        in_the_open = self.duel({}, spell="Searing Ray", book=book).act(
            Action(kind=ActionKind.CAST, spell="Searing Ray", slot_level=1,
                   target="Goblin"),
            FixedRandom(12),
        )
        assert next(
            e for e in in_the_open if e.kind == "spell_effect"
        ).data["saved"] is False

        behind_cover = self.duel(
            {(2, 1): "half-cover"}, spell="Searing Ray", book=book
        ).act(
            Action(kind=ActionKind.CAST, spell="Searing Ray", slot_level=1,
                   target="Goblin"),
            FixedRandom(12),
        )
        shielded = next(e for e in behind_cover if e.kind == "spell_effect")
        assert shielded.data["saved"] is True
        assert shielded.data["cover"] == 1

    def test_a_non_dexterity_named_save_gets_no_cover_bonus(self) -> None:
        # Hold Person saves on Wisdom. The goblin's half cover is reported but
        # grants nothing: natural 12 + 1 (Wis) = 13 still fails DC 15, where a
        # wrongly-applied +2 would save at 15.
        covered = self.duel({(2, 1): "half-cover"}, spell="Hold Person")
        covered.creatures["Wren"].spell_slots = {2: 1}
        events = covered.act(
            Action(kind=ActionKind.CAST, spell="Hold Person", slot_level=2,
                   target="Goblin"),
            FixedRandom(12),
        )
        effect = next(e for e in events if e.kind == "spell_effect")
        assert effect.data["cover"] == 1
        assert effect.data["saved"] is False

    def test_a_storey_seals_a_spell_as_it_seals_an_arrow(self) -> None:
        """The field symptom, named: a floor stopped weapons and not spells.

        ``_cover_from_square`` has always returned TOTAL across levels, but only
        the weapon path consulted it — so a cleric could shoot anything on any
        storey from anywhere while the archer beside her could not, and a map
        author reading "levels give total cover" was told something true of half
        the actions.
        """
        rng = Random(3)
        wren = caster(position=(0, 0))
        wren.spells = ("Guiding Bolt",)
        wren.spell_slots = {1: 1}
        upstairs = make_monster("Goblin Warrior", label="Upstairs", position=(15, 0))
        upstairs.level = 1
        encounter = Encounter(
            [wren, upstairs], rng, spellbook=spellbook(), battle_map=tower()
        )
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="Upstairs.*total cover"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Guiding Bolt", slot_level=1,
                       target="Upstairs"),
                FixedRandom(20),
            )
        assert upstairs.hp == upstairs.max_hp
        assert wren.spell_slots[1] == 1

    def test_the_refusal_names_which_of_several_targets_is_sealed(self) -> None:
        """A multi-target cast says *who* it cannot reach, not just that it failed.

        The whole cast is refused rather than quietly shrinking to the reachable
        names: a caller who aimed at three creatures and silently hit two has been
        given a wrong answer, not a partial one.
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
        rng = Random(3)
        wren = caster(position=(0, 5))
        wren.spells = ("Twin Bolt",)
        wren.spell_slots = {1: 1}
        encounter = Encounter(
            [
                wren,
                make_monster("Goblin Warrior", label="Open", position=(5, 5)),
                make_monster("Goblin Warrior", label="Sealed", position=(20, 5)),
            ],
            rng,
            spellbook={"Twin Bolt": twin},
            battle_map=strip(5, 3, terrain=self.WALL_COLUMN),
        )
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="Sealed.*total cover"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Twin Bolt", slot_level=1,
                       targets=("Open", "Sealed")),
                FixedRandom(20),
            )
        assert wren.spell_slots[1] == 1


class TestInteract:
    def corridor(self) -> tuple[Encounter, Random]:
        """A doorway in an otherwise solid wall: walls above and below, door in
        the middle row, archer on one side and goblin on the other."""
        rng = Random(3)
        door = MapFeature(name="door", square=(1, 1))
        encounter = Encounter(
            [archer(position=(0, 5)),
             make_monster("Goblin Warrior", label="Goblin", position=(15, 5))],
            rng,
            battle_map=strip(
                4, 3, terrain={(1, 0): "wall", (1, 2): "wall"}, features=(door,)
            ),
        )
        advance_to(encounter, "Sylvi", rng)
        return encounter, rng

    def test_a_closed_door_blocks_sight_and_passage_until_opened(self) -> None:
        encounter, rng = self.corridor()
        assert encounter.cover_between("Sylvi", "Goblin") is CoverGrade.TOTAL
        with pytest.raises(EncounterError, match="no route"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 5)), rng)

        events = encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)
        assert events[0].kind == "interact"
        assert events[0].data == {"feature": "door", "open": True}
        assert encounter.state()["map"]["features"]["door"]["open"] is True
        assert encounter.cover_between("Sylvi", "Goblin") is CoverGrade.NONE
        encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 5)), rng)
        assert encounter.creatures["Sylvi"].position == (10, 5)

    def test_interacting_is_free_but_only_once_per_turn(self) -> None:
        encounter, rng = self.corridor()
        encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)
        assert not encounter.state()["turn_state"]["action_used"]
        with pytest.raises(EncounterError, match="already interacted"):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)

    def test_the_same_creature_can_close_it_again_next_turn(self) -> None:
        encounter, rng = self.corridor()
        encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)
        for _ in range(4):
            encounter.advance(rng)
            if encounter.current_name == "Sylvi":
                break
        events = encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)
        assert events[0].data == {"feature": "door", "open": False}

    def test_a_feature_out_of_reach_is_refused(self) -> None:
        rng = Random(3)
        door = MapFeature(name="far door", square=(3, 0))
        encounter = Encounter(
            [archer(), make_monster("Goblin Warrior", label="Goblin",
                                    position=(20, 0))],
            rng,
            battle_map=strip(5, features=(door,)),
        )
        advance_to(encounter, "Sylvi", rng)
        with pytest.raises(EncounterError, match="out of reach"):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="far door"), rng)

    def test_an_unknown_feature_lists_what_the_map_has(self) -> None:
        encounter, rng = self.corridor()
        with pytest.raises(EncounterError, match="the map has: door"):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="portcullis"), rng)

    def test_interacting_without_a_map_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="no battle map"):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)


class TestReachAcrossStoreys:
    """A fixture is reached on its own storey, not merely at its own square.

    ``Encounter.battle_map.features`` merges every plane into one name table,
    so a reach test that compares squares alone lets a creature on the ground
    work a hatch directly above its head.
    """

    def two_storeys(self) -> tuple[Encounter, Random]:
        rng = Random(3)
        hatch = MapFeature(name="hatch", square=(0, 0))
        battle_map = BattleMap(
            name="tower",
            width=4,
            height=1,
            levels=MappingProxyType(
                {
                    0: MapPlane(default_terrain="floor", connectors={(1, 0): 1}),
                    1: MapPlane(
                        default_terrain="floor",
                        default_elevation=10,
                        features={"hatch": hatch},
                        connectors={(1, 0): 0},
                    ),
                }
            ),
            provenance=FIXTURE,
        )
        encounter = Encounter(
            [fighter(position=(0, 0)),
             make_monster("Goblin Warrior", label="Goblin", position=(15, 0))],
            rng,
            battle_map=battle_map,
        )
        advance_to(encounter, "Thora", rng)
        return encounter, rng

    def test_a_fixture_one_storey_up_is_out_of_reach(self) -> None:
        encounter, rng = self.two_storeys()
        assert encounter.creatures["Thora"].level == 0
        assert encounter.battle_map is not None
        assert encounter.battle_map.level_of("hatch") == 1
        with pytest.raises(EncounterError, match="cannot reach it from another storey"):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="hatch"), rng)
        assert encounter.state()["map"]["features"]["hatch"]["open"] is False

    def test_climbing_to_its_storey_brings_it_into_reach(self) -> None:
        encounter, rng = self.two_storeys()
        encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(5, 0), to_level=1), rng
        )
        encounter.act(Action(kind=ActionKind.INTERACT, feature="hatch"), rng)
        assert encounter.state()["map"]["features"]["hatch"]["open"] is True


class TestActionRecordsReplayEverything:
    """``ActionRecord.as_dict`` must carry every field ``act`` was given.

    The record is the unit of replay, and its field list is written out by
    hand — so a field added to :class:`Action` and forgotten here is silently
    dropped from a log that promises to reproduce the fight exactly.
    """

    def test_a_cross_storey_move_records_the_level_it_ended_on(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng, battle_map=tower()
        )
        advance_to(encounter, "Thora", rng)
        encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(5, 0), to_level=1), rng
        )
        assert encounter.creatures["Thora"].level == 1
        action = encounter.actions[-1].as_dict()["action"]
        assert action["to_level"] == 1

    def test_a_move_that_stays_put_records_no_level(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng, battle_map=tower()
        )
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.MOVE, to_position=(5, 0)), rng)
        assert "to_level" not in encounter.actions[-1].as_dict()["action"]


#: A metal spike takes a raw Strength check — creatures have no skill training,
#: so the DC is set as if untrained. ``fighter`` has Strength 16, a +3 modifier:
#: ``FixedRandom(15)`` clears this and ``FixedRandom(5)`` does not.
SPIKE_CHECK = FeatureCheck(ability=Ability.STRENGTH, dc=15)


def spike(name: str, square: Square) -> MapFeature:
    """One of the two spikes pinning the sluice gate.

    Its own square reads the same in both states, which is the case a fixture
    that changes nothing where it stands has to get right: pulling it moves
    terrain nowhere, only the gate's prerequisites.
    """
    return MapFeature(
        name=name,
        square=square,
        kind="spike",
        closed_terrain="floor",
        open_terrain="floor",
        costs_action=True,
        check=SPIKE_CHECK,
    )


def sluice(
    *,
    requires: tuple[str, ...] = ("north spike", "south spike"),
    gate_check: FeatureCheck | None = None,
) -> BattleMap:
    """The driving fixture: a gate that floods a room and starts a wheel turning.

    Eight by three of floor. The two spikes flank the gate at ``(2, 1)``; east
    of it ``(4, 1)`` and ``(5, 1)`` become water five feet lower, and ``(6, 1)``
    is the mill wheel, difficult ground that turns impassable. One flip, three
    kinds of change.
    """
    gate = MapFeature(
        name="sluice gate",
        square=(2, 1),
        requires=requires,
        costs_action=True,
        check=gate_check,
        affects=(
            FeatureOverlay(
                squares=((4, 1), (5, 1)),
                terrain=TerrainPair(closed="floor", open="water"),
                elevation=HeightPair(closed=0, open=-5),
            ),
            FeatureOverlay(
                squares=((6, 1),),
                terrain=TerrainPair(closed="difficult", open="mountain"),
            ),
        ),
    )
    return BattleMap.flat(
        name="sluice",
        width=8,
        height=3,
        default_terrain="floor",
        features={
            feature.name: feature
            for feature in (
                spike("north spike", (2, 0)),
                spike("south spike", (2, 2)),
                gate,
            )
        },
        provenance=FIXTURE,
    )


class TestMapFixtures:
    """Operable fixtures that move the ground under a running fight."""

    def fight(self, battle_map: BattleMap | None = None) -> tuple[Encounter, Random]:
        """Two at the spikes, two in the room the sluice floods."""
        rng = Random(3)
        encounter = Encounter(
            [
                fighter("Thora", position=square_center((1, 0))),
                fighter("Brute", position=square_center((1, 2))),
                fighter("Wader", team="foes", position=square_center((4, 1))),
                fighter("Miller", team="foes", position=square_center((6, 1))),
            ],
            rng,
            battle_map=sluice() if battle_map is None else battle_map,
        )
        return encounter, rng

    def pull_both_spikes(self, encounter: Encounter, rng: Random) -> None:
        advance_to(encounter, "Thora", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="north spike"), FixedRandom(15)
        )
        advance_to(encounter, "Brute", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="south spike"), FixedRandom(15)
        )

    def open_the_sluice(self, encounter: Encounter, rng: Random) -> None:
        self.pull_both_spikes(encounter, rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.INTERACT, feature="sluice gate"), rng)

    def route_cost(self, encounter: Encounter, name: str, goal: Square) -> int | None:
        path = encounter.route(name, goal)
        return None if path is None else path.cost_feet

    def elevation_of(self, encounter: Encounter, name: str) -> int:
        state = next(
            c for c in encounter.state()["combatants"] if c["name"] == name
        )
        return int(state["elevation"])

    # --- the driving scenario ---------------------------------------------
    def test_the_gate_floods_the_room_once_both_spikes_are_out(self) -> None:
        encounter, rng = self.fight()
        assert self.route_cost(encounter, "Wader", (5, 1)) == 5
        assert self.elevation_of(encounter, "Wader") == 0
        # Floor then difficult ground: the wheel is walkable while it is still.
        assert self.route_cost(encounter, "Wader", (6, 1)) == 5 + 10

        self.open_the_sluice(encounter, rng)

        # Terrain: the room walks like water, at twice the price.
        assert self.route_cost(encounter, "Wader", (5, 1)) == 10
        # Height: the water sits five feet below the floor it replaced.
        assert self.elevation_of(encounter, "Wader") == -5
        # And the wheel, on the same flip, is no longer ground at all.
        assert self.route_cost(encounter, "Wader", (6, 1)) is None
        # The wheel overlay carries no height pair, so its square keeps the
        # plane's own: an absent pair falls through rather than reading zero.
        assert self.elevation_of(encounter, "Miller") == 0

    def test_the_gate_refuses_until_both_spikes_are_out(self) -> None:
        encounter, rng = self.fight()
        advance_to(encounter, "Thora", rng)
        with pytest.raises(
            EncounterError, match="until north spike, south spike are open"
        ):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="sluice gate"), rng)
        # Refused before the spend: the party learns why without paying.
        assert not encounter.state()["turn_state"]["action_used"]

        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="north spike"), FixedRandom(15)
        )
        advance_to(encounter, "Brute", rng)
        with pytest.raises(EncounterError, match="until south spike is open"):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="sluice gate"), rng)

    def test_a_creature_standing_where_the_ground_turns_impassable_stays(self) -> None:
        """Entry cost governs entering, not remaining. No forced move exists."""
        encounter, rng = self.fight()
        self.open_the_sluice(encounter, rng)
        miller = encounter.creatures["Miller"]
        # Nothing shoved it and nothing refused the flip on its account.
        assert miller.position == square_center((6, 1))
        assert self.route_cost(encounter, "Miller", (6, 1)) == 0

        advance_to(encounter, "Miller", rng)
        encounter.act(
            Action(kind=ActionKind.MOVE, to_position=square_center((7, 1))), rng
        )
        assert miller.position == square_center((7, 1))
        # And having stepped off it, it may not step back on.
        assert self.route_cost(encounter, "Miller", (6, 1)) is None

    def test_two_fights_over_one_map_do_not_share_its_state(self) -> None:
        """A ``BattleMap`` is frozen but its planes hold plain dicts.

        ``simulate_rounds`` hands one map to every iteration, so a fixture
        that wrote through to the map would leak the first fight's flood into
        the second.
        """
        battle_map = sluice()
        first, first_rng = self.fight(battle_map)
        second, _ = self.fight(battle_map)
        self.open_the_sluice(first, first_rng)

        assert first.state()["map"]["features"]["sluice gate"]["open"] is True
        assert second.state()["map"]["features"]["sluice gate"]["open"] is False
        assert self.route_cost(second, "Wader", (5, 1)) == 5
        assert self.elevation_of(second, "Wader") == 0

    # --- what a claim does and does not decide ----------------------------
    def test_an_overlay_without_terrain_leaves_the_ground_it_finds(self) -> None:
        """An absent terrain pair falls through to the plane's own sparse layer.

        The riser only lifts ``(3, 0)``. That square is authored difficult, and
        it must still walk like difficult ground in both states — closed it
        costs the doubled 10 ft, open it costs the doubled 10 ft plus a 10-foot
        climb charged at the *difficult* rate of 3 ft per foot.
        """
        riser = MapFeature(
            name="riser",
            square=(1, 0),
            closed_terrain="floor",
            open_terrain="floor",
            affects=(
                FeatureOverlay(
                    squares=((3, 0),), elevation=HeightPair(closed=0, open=10)
                ),
            ),
        )
        rng = Random(3)
        encounter = Encounter(
            [fighter(), fighter("Brute", team="foes", position=square_center((5, 0)))],
            rng,
            battle_map=strip(6, terrain={(3, 0): "difficult"}, features=(riser,)),
        )
        assert self.route_cost(encounter, "Thora", (3, 0)) == 5 + 5 + 10

        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.INTERACT, feature="riser"), rng)
        assert self.route_cost(encounter, "Thora", (3, 0)) == 5 + 5 + (10 + 30)

    def test_a_square_no_fixture_claims_keeps_the_plane_underneath(self) -> None:
        """Row 0 lies outside every overlay, and the spike changes nothing.

        Six squares of plain floor at 5 ft each, before the flood and after it.
        The walk crosses the north spike's own square, which is the case a
        fixture that changes no ground has to get right.
        """
        encounter, rng = self.fight()
        assert self.route_cost(encounter, "Thora", (7, 0)) == 6 * 5
        self.open_the_sluice(encounter, rng)
        assert self.route_cost(encounter, "Thora", (7, 0)) == 6 * 5

    # --- what the map may not say -----------------------------------------
    def two_fighters(self) -> list[Creature]:
        return [fighter(), fighter("Brute", team="foes", position=square_center((5, 0)))]

    def test_an_overlay_naming_unknown_terrain_is_refused(self) -> None:
        gate = MapFeature(
            name="gate",
            square=(1, 0),
            affects=(
                FeatureOverlay(
                    squares=((3, 0),),
                    terrain=TerrainPair(closed="floor", open="vale-lava"),
                ),
            ),
        )
        with pytest.raises(EncounterError, match="does not define: vale-lava"):
            Encounter(
                self.two_fighters(), Random(1), battle_map=strip(6, features=(gate,))
            )

    def test_an_overlay_cell_off_the_map_is_refused(self) -> None:
        gate = MapFeature(
            name="gate",
            square=(1, 0),
            affects=(
                FeatureOverlay(
                    squares=((9, 0),),
                    terrain=TerrainPair(closed="floor", open="water"),
                ),
            ),
        )
        with pytest.raises(
            EncounterError, match=r"feature 'gate' reaches \(9, 0\), off the 6x1 map"
        ):
            Encounter(
                self.two_fighters(), Random(1), battle_map=strip(6, features=(gate,))
            )

    def test_two_plain_features_on_one_square_are_still_refused(self) -> None:
        with pytest.raises(
            EncounterError,
            match=r"features 'north door' and 'south door' share square \(2, 0\)",
        ):
            Encounter(
                self.two_fighters(),
                Random(1),
                battle_map=strip(
                    6,
                    features=(
                        MapFeature(name="north door", square=(2, 0)),
                        MapFeature(name="south door", square=(2, 0)),
                    ),
                ),
            )

    def test_an_overlay_reaching_another_fixtures_square_is_refused(self) -> None:
        gate = MapFeature(
            name="gate",
            square=(1, 0),
            affects=(
                FeatureOverlay(
                    squares=((3, 0),),
                    terrain=TerrainPair(closed="floor", open="water"),
                ),
            ),
        )
        lever = MapFeature(name="lever", square=(3, 0))
        with pytest.raises(
            EncounterError, match=r"features 'gate' and 'lever' share square \(3, 0\)"
        ):
            Encounter(
                self.two_fighters(),
                Random(1),
                battle_map=strip(6, features=(gate, lever)),
            )

    def test_a_fixture_claiming_its_own_square_twice_is_refused(self) -> None:
        gate = MapFeature(
            name="gate",
            square=(1, 0),
            affects=(
                FeatureOverlay(
                    squares=((1, 0),),
                    terrain=TerrainPair(closed="floor", open="water"),
                ),
            ),
        )
        with pytest.raises(
            EncounterError, match=r"feature 'gate' claims square \(1, 0\) twice"
        ):
            Encounter(
                self.two_fighters(), Random(1), battle_map=strip(6, features=(gate,))
            )

    def test_one_square_may_be_claimed_once_on_each_storey(self) -> None:
        """The rule is one claim per square *per level*, not per footprint."""
        battle_map = BattleMap(
            name="tower",
            width=4,
            height=1,
            levels=MappingProxyType(
                {
                    0: MapPlane(
                        default_terrain="floor",
                        features={"ground door": MapFeature("ground door", (0, 0))},
                        connectors={(1, 0): 1},
                    ),
                    1: MapPlane(
                        default_terrain="floor",
                        features={"upper door": MapFeature("upper door", (0, 0))},
                        connectors={(1, 0): 0},
                    ),
                }
            ),
            provenance=FIXTURE,
        )
        encounter = Encounter(
            [fighter(position=square_center((2, 0))),
             fighter("Brute", team="foes", position=square_center((3, 0)))],
            Random(1),
            battle_map=battle_map,
        )
        assert sorted(encounter.state()["map"]["features"]) == [
            "ground door", "upper door"
        ]

    def test_a_prerequisite_the_map_does_not_have_is_refused(self) -> None:
        gate = MapFeature(name="gate", square=(1, 0), requires=("ghost lever",))
        lever = MapFeature(name="lever", square=(3, 0))
        with pytest.raises(
            EncounterError,
            match=(
                r"feature 'gate' requires 'ghost lever', which this map does not "
                r"have; the map has: gate, lever"
            ),
        ):
            Encounter(
                self.two_fighters(),
                Random(1),
                battle_map=strip(6, features=(gate, lever)),
            )

    def test_a_prerequisite_on_another_storey_resolves(self) -> None:
        """``requires`` is a prerequisite, not a reach: it may cross a floor."""
        battle_map = BattleMap(
            name="tower",
            width=4,
            height=1,
            levels=MappingProxyType(
                {
                    0: MapPlane(
                        default_terrain="floor",
                        features={
                            "gate": MapFeature(
                                "gate", (0, 0), requires=("upper lever",)
                            )
                        },
                        connectors={(1, 0): 1},
                    ),
                    1: MapPlane(
                        default_terrain="floor",
                        features={"upper lever": MapFeature("upper lever", (3, 0))},
                        connectors={(1, 0): 0},
                    ),
                }
            ),
            provenance=FIXTURE,
        )
        encounter = Encounter(
            [fighter(position=square_center((2, 0))),
             fighter("Brute", team="foes", position=square_center((3, 0)))],
            Random(1),
            battle_map=battle_map,
        )
        assert encounter.state()["map"]["features"]["gate"]["blocked_by"] == [
            "upper lever"
        ]

    # --- what operating one costs -----------------------------------------
    def test_a_fixture_that_costs_an_action_spends_the_action(self) -> None:
        encounter, rng = self.fight()
        advance_to(encounter, "Thora", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="north spike"), FixedRandom(15)
        )
        turn = encounter.state()["turn_state"]
        assert turn["action_used"] is True
        # It spends the action *instead of* the free interaction, not as well.
        assert turn["interaction_used"] is False

    def test_a_fixture_that_costs_an_action_is_refused_without_one(self) -> None:
        encounter, rng = self.fight()
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.DODGE), rng)
        with pytest.raises(EncounterError, match="already taken an action this turn"):
            encounter.act(
                Action(kind=ActionKind.INTERACT, feature="north spike"), FixedRandom(15)
            )

    def test_a_failed_check_spends_the_action_and_moves_nothing(self) -> None:
        encounter, rng = self.fight()
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.INTERACT, feature="north spike"), FixedRandom(5)
        )
        assert events[0].kind == "interact"
        assert events[0].data == {
            "feature": "north spike",
            "open": False,
            "success": False,
            "check": "d20 [5] +3 = 8 vs DC 15",
        }
        assert encounter.state()["map"]["features"]["north spike"]["open"] is False
        assert encounter.state()["turn_state"]["action_used"] is True

    def test_a_passed_check_reports_the_roll_beside_the_result(self) -> None:
        encounter, rng = self.fight()
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.INTERACT, feature="north spike"), FixedRandom(15)
        )
        assert events[0].data == {
            "feature": "north spike",
            "open": True,
            "success": True,
            "check": "d20 [15] +3 = 18 vs DC 15",
        }
        assert "d20 [15]" in events[0].detail

    def test_a_fixture_with_no_check_reports_no_roll(self) -> None:
        """The common case's event dict stays exactly what it was."""
        encounter, rng = self.fight()
        self.pull_both_spikes(encounter, rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.INTERACT, feature="sluice gate"), rng
        )
        assert events[0].data == {"feature": "sluice gate", "open": True}

    # --- saying which way to move it --------------------------------------
    def test_set_open_makes_it_so_rather_than_toggling(self) -> None:
        encounter, rng = self.fight()
        self.pull_both_spikes(encounter, rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="sluice gate", set_open=True), rng
        )
        assert encounter.state()["map"]["features"]["sluice gate"]["open"] is True

    def test_set_open_matching_the_current_state_is_refused(self) -> None:
        encounter, rng = self.fight()
        self.open_the_sluice(encounter, rng)
        advance_to(encounter, "Brute", rng)
        with pytest.raises(EncounterError, match="sluice gate is already open"):
            encounter.act(
                Action(kind=ActionKind.INTERACT, feature="sluice gate", set_open=True),
                rng,
            )
        turn = encounter.state()["turn_state"]
        assert turn["action_used"] is False
        assert turn["interaction_used"] is False

    def test_closing_something_already_closed_is_refused(self) -> None:
        encounter, rng = self.corridor_fight()
        with pytest.raises(EncounterError, match="door is already closed"):
            encounter.act(
                Action(kind=ActionKind.INTERACT, feature="door", set_open=False), rng
            )
        assert encounter.state()["turn_state"]["interaction_used"] is False

    def corridor_fight(self) -> tuple[Encounter, Random]:
        rng = Random(3)
        door = MapFeature(name="door", square=(1, 0))
        encounter = Encounter(
            [fighter(), fighter("Brute", team="foes", position=square_center((5, 0)))],
            rng,
            battle_map=strip(6, features=(door,)),
        )
        advance_to(encounter, "Thora", rng)
        return encounter, rng

    def test_the_record_carries_which_way_it_was_asked_to_move(self) -> None:
        encounter, rng = self.fight()
        self.pull_both_spikes(encounter, rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="sluice gate", set_open=True), rng
        )
        action = encounter.actions[-1].as_dict()["action"]
        assert action["set_open"] is True
        assert action["feature"] == "sluice gate"

    def test_a_toggle_records_no_direction(self) -> None:
        encounter, rng = self.corridor_fight()
        encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)
        assert "set_open" not in encounter.actions[-1].as_dict()["action"]

    # --- closing is never gated -------------------------------------------
    def test_driving_a_spike_back_in_does_not_shut_the_gate(self) -> None:
        encounter, rng = self.fight()
        self.open_the_sluice(encounter, rng)
        advance_to(encounter, "Brute", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="south spike", set_open=False),
            FixedRandom(15),
        )
        features = encounter.state()["map"]["features"]
        assert features["south spike"]["open"] is False
        assert features["sluice gate"]["open"] is True
        assert self.route_cost(encounter, "Wader", (5, 1)) == 10

    def test_the_gate_may_be_closed_with_its_prerequisites_unmet(self) -> None:
        encounter, rng = self.fight()
        self.open_the_sluice(encounter, rng)
        advance_to(encounter, "Brute", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="south spike", set_open=False),
            FixedRandom(15),
        )
        advance_to(encounter, "Wader", rng)
        # Wader is not next to the gate; Miller is not either. Come round to
        # Brute, whose spike is back in and whose action is fresh.
        advance_to(encounter, "Brute", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="sluice gate", set_open=False), rng
        )
        assert encounter.state()["map"]["features"]["sluice gate"]["open"] is False
        assert self.route_cost(encounter, "Wader", (5, 1)) == 5

    # --- what the state block says ----------------------------------------
    def test_the_state_block_describes_a_fixture_beyond_a_plain_door(self) -> None:
        encounter, rng = self.fight()
        features = encounter.state()["map"]["features"]
        assert features["north spike"] == {
            "square": [2, 0],
            "kind": "spike",
            "level": 0,
            "open": False,
            "costs_action": True,
            "check": {"ability": "strength", "dc": 15},
        }
        assert features["sluice gate"] == {
            "square": [2, 1],
            "kind": "door",
            "level": 0,
            "open": False,
            "affects": [[4, 1], [5, 1], [6, 1]],
            "requires": ["north spike", "south spike"],
            "blocked_by": ["north spike", "south spike"],
            "costs_action": True,
        }

    def test_what_is_blocking_it_narrows_as_the_spikes_come_out(self) -> None:
        encounter, rng = self.fight()
        self.pull_both_spikes(encounter, rng)
        gate = encounter.state()["map"]["features"]["sluice gate"]
        assert gate["requires"] == ["north spike", "south spike"]
        assert "blocked_by" not in gate

    # --- what the state block says about the ground ------------------------
    def test_the_map_elevation_summary_falls_with_the_flood(self) -> None:
        """One payload cannot be half live: ``features[…].open`` already is.

        The creature standing in the flooded room reports −5 through
        ``_creature_state``; a block reading the authored plane alone would
        say in the same breath that the map's lowest ground is 0.
        """
        encounter, rng = self.fight()
        assert encounter.state()["map"]["elevation"]["flat"] is True

        self.open_the_sluice(encounter, rng)

        elevation = encounter.state()["map"]["elevation"]
        assert (elevation["min"], elevation["max"]) == (-5, 0)
        assert elevation["flat"] is False
        assert self.elevation_of(encounter, "Wader") == elevation["min"]

    def test_a_claim_decides_a_square_the_plane_never_raised(self) -> None:
        """A claimed square never falls back, so it covers the plane too.

        The file raises three of the four squares; the gate decides the
        fourth in both its states, so nothing is left to read the default.
        0 ft, which no square stands at, must stay out of the range.
        """
        gate = MapFeature(
            name="floodgate",
            square=(1, 0),
            initially_open=True,
            elevation=HeightPair(closed=20, open=15),
        )
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(0, 5))],
            Random(1),
            battle_map=strip(
                2, 2, elevation={(0, 0): 10, (0, 1): 10, (1, 1): 10}, features=(gate,)
            ),
        )
        elevation = encounter.state()["map"]["elevation"]
        assert (elevation["default"], elevation["min"], elevation["max"]) == (0, 10, 15)

    def test_a_claim_does_not_let_the_default_back_into_a_covered_plane(self) -> None:
        """The ``covered`` shortcut has to survive a claim moving a height.

        Every square is raised to 10 by the file and the gate lowers its own
        to 5, so the map's range is 5 to 10. The default of 0 is still what
        no square falls back to.
        """
        gate = MapFeature(
            name="floodgate",
            square=(2, 0),
            initially_open=True,
            elevation=HeightPair(closed=10, open=5),
        )
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(5, 0))],
            Random(1),
            battle_map=strip(
                3,
                elevation={(0, 0): 10, (1, 0): 10, (2, 0): 10},
                features=(gate,),
            ),
        )
        elevation = encounter.state()["map"]["elevation"]
        assert (elevation["default"], elevation["min"], elevation["max"]) == (0, 5, 10)
        assert elevation["flat"] is False

    def test_a_claim_moves_only_its_own_storeys_summary(self) -> None:
        """``_feature_squares`` is keyed by ``(level, square)``, and read so.

        The gate is on the ground, and the gallery over it is untouched by
        it. A summary that matched on the square alone would drag −5 upstairs.
        """
        gate = MapFeature(
            name="floodgate",
            square=(2, 0),
            initially_open=True,
            affects=(
                FeatureOverlay(
                    squares=((0, 0),), elevation=HeightPair(closed=0, open=-5)
                ),
            ),
        )
        battle_map = BattleMap(
            name="flooded tower",
            width=4,
            height=1,
            levels=MappingProxyType(
                {
                    0: MapPlane(
                        default_terrain="floor",
                        features={gate.name: gate},
                        connectors={(1, 0): 1},
                    ),
                    1: MapPlane(
                        default_terrain="floor",
                        default_elevation=10,
                        connectors={(1, 0): 0},
                    ),
                }
            ),
            provenance=FIXTURE,
        )
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))],
            Random(1),
            battle_map=battle_map,
        )
        ground, gallery = encounter.state()["map"]["levels"]
        assert (ground["elevation"]["min"], ground["elevation"]["max"]) == (-5, 0)
        assert (gallery["elevation"]["min"], gallery["elevation"]["max"]) == (10, 10)
        assert gallery["elevation"]["flat"] is True


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

    def test_an_area_spell_may_be_centred_off_the_x_axis(self) -> None:
        rng = Random(4)
        wizard = caster(position=0)
        high = make_monster("Goblin Warrior", label="Goblin A", position=(100, 40))
        low = make_monster("Goblin Warrior", label="Goblin B", position=100)
        encounter = Encounter([wizard, high, low], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3,
                   center=(100, 40)),
            Random(2),
        )
        assert high.hp < high.max_hp
        assert low.hp == low.max_hp

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


class TestAoeShapes2D:
    """Golden shape resolutions through the stepper: who is caught is the test."""

    def hit_names(self, events: Sequence[Event]) -> set[str]:
        return {event.target for event in events if event.kind == "spell_effect"}

    def cast(self, encounter: Encounter, rng: Random, **aim: Any) -> set[str]:
        advance_to(encounter, "Vesna", rng)
        events = encounter.act(Action(kind=ActionKind.CAST, **aim), Random(2))
        return self.hit_names(events)

    def test_a_cone_catches_the_wedge_and_misses_a_flank(self) -> None:
        rng = Random(4)
        encounter = Encounter(
            [
                shaper(),
                make_monster("Goblin Warrior", label="Front", position=(10, 0)),
                make_monster("Goblin Warrior", label="Flank", position=(5, 10)),
            ],
            rng,
            spellbook=shaped_spellbook(),
        )
        caught = self.cast(encounter, rng, spell="Flame Fan", direction=(1, 0))
        assert caught == {"Front"}

    def test_a_cone_needs_one_of_the_eight_directions(self) -> None:
        rng = Random(4)
        encounter = Encounter(
            [shaper(), make_monster("Goblin Warrior", label="Front",
                                    position=(10, 0))],
            rng,
            spellbook=shaped_spellbook(),
        )
        advance_to(encounter, "Vesna", rng)
        with pytest.raises(EncounterError, match="unit offsets"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Flame Fan", direction=(2, 0)),
                rng,
            )

    def test_a_line_runs_down_the_corridor_it_is_aimed_along(self) -> None:
        rng = Random(4)
        encounter = Encounter(
            [
                shaper(),
                make_monster("Goblin Warrior", label="Near", position=(10, 0)),
                make_monster("Goblin Warrior", label="Far", position=(25, 0)),
                make_monster("Goblin Warrior", label="Off", position=(10, 5)),
            ],
            rng,
            spellbook=shaped_spellbook(),
        )
        caught = self.cast(encounter, rng, spell="Spark Line", toward="Far")
        assert caught == {"Near", "Far"}

    def test_a_cube_is_a_block_from_its_minimum_corner(self) -> None:
        rng = Random(4)
        encounter = Encounter(
            [
                shaper(),
                make_monster("Goblin Warrior", label="Inside", position=(15, 5)),
                make_monster("Goblin Warrior", label="Outside", position=(5, 0)),
            ],
            rng,
            spellbook=shaped_spellbook(),
        )
        caught = self.cast(encounter, rng, spell="Stone Cube", center=(10, 0))
        assert caught == {"Inside"}

    def test_a_sphere_lands_on_a_two_dimensional_cluster(self) -> None:
        rng = Random(4)
        wizard = caster(position=(0, 0))
        encounter = Encounter(
            [
                wizard,
                make_monster("Goblin Warrior", label="A", position=(100, 100)),
                make_monster("Goblin Warrior", label="B", position=(105, 105)),
                make_monster("Goblin Warrior", label="C", position=(140, 140)),
            ],
            rng,
            spellbook=spellbook(),
        )
        advance_to(encounter, "Wren", rng)
        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3,
                   center=(100, 100)),
            Random(2),
        )
        assert self.hit_names(events) == {"A", "B"}

    def test_a_wall_between_caster_and_origin_refuses_the_sphere(self) -> None:
        rng = Random(4)
        wizard = caster(position=(0, 5))
        encounter = Encounter(
            [wizard, make_monster("Goblin Warrior", label="Goblin",
                                  position=(20, 5))],
            rng,
            spellbook=spellbook(),
            battle_map=strip(
                5, 3,
                terrain={(2, 0): "wall", (2, 1): "wall", (2, 2): "wall"},
            ),
        )
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="cannot see"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3,
                       center=(20, 5)),
                rng,
            )

    def test_area_targets_is_the_membership_authority(self) -> None:
        rng = Random(4)
        encounter = Encounter(
            [
                shaper(),
                make_monster("Goblin Warrior", label="Front", position=(10, 0)),
                make_monster("Goblin Warrior", label="Flank", position=(5, 10)),
            ],
            rng,
            spellbook=shaped_spellbook(),
        )
        caught = encounter.area_targets(
            encounter.spellbook["Flame Fan"], "Vesna", direction=(1, 0)
        )
        assert [creature.name for creature in caught] == ["Front"]

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
        event = self.fireball_save(conditions=(Condition.RESTRAINED,))
        assert rolled_with(event) == "disadvantage"
        assert "auto-fail" not in event.detail

    def test_an_unhindered_target_saves_straight(self) -> None:
        assert rolled_with(self.fireball_save()) == "none"

    def test_a_paralyzed_target_still_fails_outright(self) -> None:
        assert "auto-fail" in self.fireball_save(conditions=(Condition.PARALYZED,)).detail

    def test_dodging_gives_advantage_on_a_dexterity_save(self) -> None:
        assert rolled_with(self.fireball_save(dodging=True)) == "advantage"

    def test_a_restrained_dodger_loses_the_benefit_rather_than_cancelling(self) -> None:
        # Dodge's benefits are lost while Speed is 0, and Restrained sets Speed 0.
        # Treating the Dodge as a live source of Advantage would cancel the
        # Disadvantage and hand the creature a straight roll it has not earned.
        event = self.fireball_save(conditions=(Condition.RESTRAINED,), dodging=True)
        assert rolled_with(event) == "disadvantage"

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

    def mark(self, *, position: Point | int, conditions: Sequence[str] = ()) -> Creature:
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

    def test_an_unhindered_target_is_attacked_straight(self) -> None:
        assert rolled_with(self.bolt()) == "none"

    def test_a_paralyzed_target_grants_advantage(self) -> None:
        event = self.bolt(target_conditions=(Condition.PARALYZED,))
        assert rolled_with(event) == "advantage"

    def test_a_restrained_target_grants_advantage(self) -> None:
        event = self.bolt(target_conditions=(Condition.RESTRAINED,))
        assert rolled_with(event) == "advantage"

    def test_a_blinded_caster_attacks_with_disadvantage(self) -> None:
        event = self.bolt(caster_conditions=(Condition.BLINDED,))
        assert rolled_with(event) == "disadvantage"

    def test_a_frightened_caster_attacks_with_disadvantage(self) -> None:
        event = self.bolt(caster_conditions=(Condition.FRIGHTENED,))
        assert rolled_with(event) == "disadvantage"

    def test_a_dodging_target_imposes_disadvantage(self) -> None:
        # SRD 5.2, Dodge: "any attack roll made against you has Disadvantage if
        # you can see the attacker". The _dodging map was never consulted on the
        # cast path, so a Dodge bought nothing against a spell.
        assert rolled_with(self.bolt(dodging=True)) == "disadvantage"

    def test_a_blinded_caster_on_a_paralyzed_target_cancels_to_neither(self) -> None:
        event = self.bolt(
            caster_conditions=(Condition.BLINDED,),
            target_conditions=(Condition.PARALYZED,),
        )
        assert rolled_with(event) == "none"

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
        assert rolled_with(event) == "advantage"

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
        assert rolled_with(near) == "advantage"
        assert rolled_with(far) == "disadvantage"

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

    def test_the_two_paths_agree_under_a_five_ten_five_diagonal(self) -> None:
        # The fight's DiagonalRule threads through every distance the stepper
        # consults, and an off-axis Prone target is where a dropped rule shows:
        # (5, 5) reads as 5 ft under the default 5-5-5 but 7 ft under this
        # fight's 5-10-5, so Prone's within-5-feet clause flips with the rule.
        # The cast path measured under the default, reading Advantage where the
        # swing path read Disadvantage for the same geometry.
        rng = Random(4)
        wren = self.bolt_caster()
        target = self.mark(position=(5, 5), conditions=(Condition.PRONE,))
        encounter = Encounter(
            [wren, target],
            rng,
            spellbook=spellbook(),
            movement_rule=DiagonalRule.FIVE_TEN_FIVE,
        )
        dagger = wren.attacks[0]
        assert encounter.spell_attack_advantage(wren, target) == encounter.attack_advantage(
            wren, target, dagger
        )
        assert encounter.spell_attack_advantage(wren, target) is Advantage.DISADVANTAGE


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

    def test_the_release_happens_before_the_new_spell_resolves(self) -> None:
        """Recasting at the old victim resolves against the post-release state.

        "The moment you start casting" is a *when*, not just a *whether*: by the
        time the new spell rolls its saves, the old spell's conditions are gone.
        Releasing after resolution instead let a caster chain-lock its own
        victim — the paralysis the first cast was still holding auto-failed the
        second cast's Dexterity save, whatever the die said. The end state
        cannot see this (the release still happened, just too late), so the
        pin is the save itself: a forced 19 + 2 beats DC 15, and there is no
        natural-20 auto-success on saves to blur what is being tested. No
        bundled concentration spell forces a Dexterity save, so this needs a
        fixture spell.
        """
        snare = Spell(
            name="Snare",
            level=1,
            save_ability=Ability.DEXTERITY,
            condition=str(Condition.RESTRAINED),
            range_feet=60,
            concentration=True,
            provenance=FIXTURE,
        )
        wren = caster(position=0)
        wren.spells = ("Hold Person", "Snare")
        wren.spell_slots = {1: 1, 2: 1}
        victim = fighter("Bandit0", team="foes", position=10)
        victim.abilities[Ability.WISDOM] = 6
        rng = Random(11)
        book = spellbook()
        book["Snare"] = snare
        encounter = Encounter([wren, victim], rng, spellbook=book)
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Hold Person", target="Bandit0"),
            FixedRandom(1),
        )
        assert Condition.PARALYZED in victim.conditions

        self.their_turn(encounter, rng, "Wren")
        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Snare", target="Bandit0"),
            FixedRandom(19),
        )
        detail = detail_of(events, "spell_effect")
        assert "auto-fail" not in detail, "the lapsed paralysis must not decide the save"
        assert "saved" in detail
        assert Condition.RESTRAINED not in victim.conditions
        assert Condition.PARALYZED not in victim.conditions
        assert wren.concentrating_on == "Snare"
        # The release is announced before the cast, because that is when it
        # happened: Concentration ends the moment the casting starts.
        assert kinds(events).index("effect_end") < kinds(events).index("cast")

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
