"""Analytics tests.

The load-bearing one is ``test_one_iteration_matches_a_single_hand_driven_run``.
If a batch of one ever stops matching a single encounter at the same seed, the
analytics have drifted away from the rules live play uses, and every number they
produce is suspect.
"""

from __future__ import annotations

from collections.abc import Sequence
from random import Random
from typing import Any

import pytest

from fivee_sim.analytics.expectation import attack_damage_expectation
from fivee_sim.analytics.montecarlo import (
    MAX_ACTIONS_PER_TURN,
    _attack_options,
    _spell_options,
    auto_action,
    run_encounter,
    simulate_dpr,
    simulate_rounds,
    summarise,
)
from fivee_sim.content import make_monster, spellbook
from fivee_sim.kernel.actions import AttackKind
from fivee_sim.kernel.conditions import EFFECTS, Condition, ConditionEffect
from fivee_sim.kernel.dice import Advantage, Dice
from fivee_sim.kernel.grid import as_point, distance_feet
from fivee_sim.kernel.items import ItemEffect
from fivee_sim.kernel.rules import Ability, DamageType
from fivee_sim.model.battlemap import BattleMap
from fivee_sim.model.creature import AttackOption, Creature, DeathRule
from fivee_sim.model.encounter import Action, ActionKind, Encounter

from .conftest import (
    FixedRandom,
    advance_to,
    caster,
    fighter,
    shaped_spellbook,
    shaper,
)

SEED = 20260730


def duel() -> Sequence[Creature]:
    return [
        fighter("Thora", position=0),
        make_monster("Goblin Warrior", label="Goblin", position=15),
    ]


def melee_brawl() -> Sequence[Creature]:
    return [
        fighter("Thora", position=0),
        make_monster("Goblin Warrior", label="Goblin A", position=10),
        make_monster("Goblin Warrior", label="Goblin B", position=15),
    ]


class TestReproducibility:
    def test_the_same_seed_produces_an_identical_transcript(self) -> None:
        def transcript(seed: int) -> list[tuple[str, str, str, str]]:
            rng = Random(seed)
            encounter = Encounter(list(duel()), rng, spellbook=spellbook())
            run_encounter(encounter, rng, max_rounds=20)
            return [
                (event.kind, event.actor, event.target, event.detail)
                for event in encounter.log
            ]

        assert transcript(SEED) == transcript(SEED)

    def test_different_seeds_diverge(self) -> None:
        def outcome(seed: int) -> str:
            rng = Random(seed)
            encounter = Encounter(list(duel()), rng, spellbook=spellbook())
            run_encounter(encounter, rng, max_rounds=20)
            return "|".join(event.detail for event in encounter.log)

        assert outcome(SEED) != outcome(SEED + 1)


class TestStatefulAndBatchAgree:
    def test_one_iteration_matches_a_single_hand_driven_run(self) -> None:
        rng = Random(SEED)
        encounter = Encounter(list(duel()), rng, spellbook=spellbook())
        manual = run_encounter(encounter, rng, max_rounds=20)

        batch = simulate_rounds(
            duel, iterations=1, seed=SEED, max_rounds=20, spellbook=spellbook()
        )

        expected_winner = manual.winner if manual.winner is not None else "none"
        assert batch["wins"] == {expected_winner: 1}
        assert batch["rounds"]["mean"] == float(manual.rounds)
        assert batch["rounds"]["min"] == batch["rounds"]["max"] == float(manual.rounds)


class TestSimulateRounds:
    def test_win_rates_cover_every_iteration(self) -> None:
        result = simulate_rounds(
            melee_brawl, iterations=60, seed=SEED, max_rounds=20, spellbook=spellbook()
        )
        assert sum(result["wins"].values()) == 60
        assert pytest.approx(sum(result["win_rate"].values()), abs=1e-6) == 1.0

    def test_a_fight_reaches_a_conclusion_rather_than_timing_out(self) -> None:
        result = simulate_rounds(duel, iterations=40, seed=SEED, max_rounds=20)
        assert result["timed_out"] == 0
        assert "none" not in result["wins"]

    def test_two_goblins_beat_one_fighter_more_often_than_one_does(self) -> None:
        alone = simulate_rounds(duel, iterations=80, seed=SEED, max_rounds=20)
        outnumbered = simulate_rounds(melee_brawl, iterations=80, seed=SEED, max_rounds=20)
        assert outnumbered["win_rate"].get("monsters", 0.0) > alone["win_rate"].get(
            "monsters", 0.0
        )

    def test_iterations_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            simulate_rounds(duel, iterations=0, seed=SEED)


class TestSimulateDpr:
    def test_damage_accumulates_over_the_requested_rounds(self) -> None:
        result = simulate_dpr(
            lambda: fighter("Thora"), target_ac=15, rounds=3, iterations=200, seed=SEED
        )
        assert result["damage"]["mean"] > 0
        # Both figures are reported rounded to three decimals, so they agree to
        # within rounding rather than exactly.
        assert result["damage_per_round"] == pytest.approx(
            result["damage"]["mean"] / 3, abs=1e-3
        )

    def test_a_higher_armour_class_takes_less_damage(self) -> None:
        soft = simulate_dpr(
            lambda: fighter("Thora"), target_ac=10, rounds=3, iterations=300, seed=SEED
        )
        armoured = simulate_dpr(
            lambda: fighter("Thora"), target_ac=20, rounds=3, iterations=300, seed=SEED
        )
        assert armoured["damage"]["mean"] < soft["damage"]["mean"]

    def test_extra_attack_raises_expected_damage(self) -> None:
        once = simulate_dpr(
            lambda: fighter("Thora"), target_ac=14, rounds=2, iterations=300, seed=SEED
        )
        twice = simulate_dpr(
            lambda: fighter("Thora", attacks_per_action=2),
            target_ac=14,
            rounds=2,
            iterations=300,
            seed=SEED,
        )
        assert twice["damage"]["mean"] > once["damage"]["mean"] * 1.5


def sword_and_spell(name: str = "Thora") -> Creature:
    """A three-attack build that also knows a spell.

    The weapon is deliberately worth more per swing than the spell (about 14.7
    against AC 15, against the bolt's 9.8), so the policy attacks first. That
    ordering is what exposes a turn budget the attacker never had: an Attack
    action it has already spent must not leave a second action in hand.
    """
    return Creature(
        name=name,
        team="party",
        ac=16,
        max_hp=60,
        speed=30,
        abilities={Ability.STRENGTH: 16, Ability.DEXTERITY: 14},
        attacks=(
            AttackOption(
                name="Greatsword",
                attack_bonus=9,
                damage=Dice(2, 8, 10),
                damage_type=DamageType.SLASHING,
                kind=AttackKind.MELEE,
                provenance="synthetic test fixture, not SRD content",
            ),
        ),
        attacks_per_action=3,
        spells=("Guiding Bolt",),
        spell_slots={1: 4},
        spell_save_dc=15,
        spell_attack_bonus=7,
        position=0,
        provenance="synthetic test fixture, not SRD content",
    )


class TestSimulateDprSpendsTheAttackersOwnTurnBudget:
    """Round 1 must run on the attacker's budget, not the initiative winner's.

    ``Encounter.__init__`` begins a turn for whoever won Initiative, and
    ``simulate_dpr`` then rewrites the order to put the attacker first. While the
    turn state was left as ``__init__`` built it, round 1 ran on the *dummy's*
    budget — ``movement_left=0``, ``attacks_left=1`` — in the 153/400 of seeds
    where the dummy won the roll, which cost swings, forfeited the round for a
    build starting out of reach, and left a spent action looking unspent.

    SRD 5.2.1 is explicit on both halves: "On your turn, you can move a distance up
    to your Speed and take one action" (Combat, "Your Turn"), and Extra Attack is
    "You can attack twice instead of once whenever you take the Attack action on
    your turn" — more swings inside one action, never a second action.
    """

    ROUNDS = 3
    TARGET_AC = 14

    @pytest.mark.parametrize("attacks_per_action", [1, 2, 3])
    def test_every_round_lands_the_full_legal_swing_count(
        self, attacks_per_action: int
    ) -> None:
        # Zero variance by construction: the dummy has 10,000 hit points so it
        # never drops, neither creature moves, and nothing else is on offer. The
        # count is therefore exactly the legal one or the budget was wrong.
        iterations = 200
        result = simulate_dpr(
            lambda: fighter("Thora", attacks_per_action=attacks_per_action),
            target_ac=self.TARGET_AC,
            rounds=self.ROUNDS,
            iterations=iterations,
            seed=SEED,
        )
        legal = attacks_per_action * self.ROUNDS * iterations
        swings = result["actions"].get("attack:Longsword", 0)
        # An Attack action grants attacks_per_action swings and no more.
        assert swings <= legal
        # And every one of them is taken.
        assert result["actions"] == {"attack:Longsword": legal}

    @pytest.mark.parametrize("attacks_per_action", [1, 2, 3])
    def test_damage_per_round_matches_the_closed_form(
        self, attacks_per_action: int
    ) -> None:
        # The oracle is the engine's own exact arithmetic, read off the same
        # fixture the run uses so the two cannot drift apart.
        option = fighter("Thora").attacks[0]
        expected = attacks_per_action * attack_damage_expectation(
            attack_bonus=option.attack_bonus,
            target_ac=self.TARGET_AC,
            damage=option.damage,
        )
        result = simulate_dpr(
            lambda: fighter("Thora", attacks_per_action=attacks_per_action),
            target_ac=self.TARGET_AC,
            rounds=self.ROUNDS,
            iterations=6_000,
            seed=SEED,
        )
        # 3% sits above four standard errors at this sample size and well below
        # the 6-8% the stale budget cost, so it discriminates rather than merely
        # passing.
        assert result["damage_per_round"] == pytest.approx(expected, rel=0.03)

    def test_a_build_starting_out_of_reach_still_closes_in_round_one(self) -> None:
        # The other half of the budget. A stale movement_left of 0 made
        # _closing_move return None, so a melee build measured at range simply
        # forfeited its first round.
        iterations = 200
        result = simulate_dpr(
            lambda: fighter("Thora"),
            target_ac=self.TARGET_AC,
            rounds=self.ROUNDS,
            iterations=iterations,
            seed=SEED,
            distance=20,
        )
        assert result["actions"]["attack:Longsword"] == self.ROUNDS * iterations

    def test_a_spent_attack_action_does_not_also_buy_a_spell(self) -> None:
        # The rules defect rather than the measurement one. _do_attack marks the
        # action used only when attacks_left falls to attacks_per_action - 1, so
        # a stale attacks_left of 1 on a three-attack build decremented to 0
        # without ever setting it — and the policy was then offered a Magic
        # action on top of the Attack action already taken.
        iterations = 200
        result = simulate_dpr(
            sword_and_spell,
            target_ac=15,
            rounds=self.ROUNDS,
            iterations=iterations,
            seed=SEED,
            spellbook=spellbook(),
        )
        assert result["actions"] == {"attack:Greatsword": 3 * self.ROUNDS * iterations}


class TestSimulateDprStaysReproducible:
    def test_the_same_seed_reproduces_the_same_result(self) -> None:
        def run(seed: int) -> dict[str, object]:
            return simulate_dpr(
                lambda: fighter("Thora", attacks_per_action=3),
                target_ac=14,
                rounds=3,
                iterations=120,
                seed=seed,
            )

        assert run(SEED) == run(SEED)
        assert run(SEED) != run(SEED + 1)

    def test_iteration_i_still_uses_seed_plus_i(self) -> None:
        def run(*, iterations: int, seed: int) -> dict[str, Any]:
            return simulate_dpr(
                lambda: fighter("Thora", attacks_per_action=3),
                target_ac=14,
                rounds=3,
                iterations=iterations,
                seed=seed,
            )

        batch = run(iterations=6, seed=SEED)
        singles = [
            run(iterations=1, seed=SEED + index)["damage"]["mean"] for index in range(6)
        ]
        assert batch["damage"]["min"] == min(singles)
        assert batch["damage"]["max"] == max(singles)
        # ``Stats.as_dict`` reports the mean rounded to three decimals, so this
        # agrees to within that rounding rather than exactly.
        assert batch["damage"]["mean"] == pytest.approx(sum(singles) / 6, abs=1e-3)


def archer(
    *, arrows: int = 1, position: int | tuple[int, int] = 0, max_hp: int = 30
) -> Creature:
    """A build with a ranged option that spends ammunition and a melee fallback."""
    build = fighter("Robin", position=position, max_hp=max_hp)
    build.attacks = (
        AttackOption(
            name="Shortbow",
            attack_bonus=5,
            damage=Dice(1, 6, 3),
            damage_type=DamageType.PIERCING,
            kind=AttackKind.RANGED,
            normal_range=80,
            long_range=320,
            ammunition="Arrow",
            provenance=FIXTURE,
        ),
        AttackOption(
            name="Dagger",
            attack_bonus=5,
            damage=Dice(1, 4, 3),
            damage_type=DamageType.PIERCING,
            kind=AttackKind.MELEE,
            provenance=FIXTURE,
        ),
    )
    build.items = {"Arrow": arrows}
    return build


class TestAmmunitionAwarePolicy:
    """A quiver running dry must not silently end the archer's fight.

    ``_attack_options`` used to filter proposed swings on reach and cover only,
    so a policy offered the same refused shot every turn, the stepper's
    ``EncounterError`` broke that turn's action loop, and the archer stood
    still for the rest of the encounter rather than drawing the dagger it also
    carries. ``_threat_range`` had the matching gap: it fed ``_closing_move``
    and ``_closing_dash`` an unfiltered 320 ft — the empty bow's own range — so
    neither ever saw a reason to close, and the archer never moved either.
    """

    def test_a_dry_quiver_sends_the_archer_to_melee_rather_than_standing_still(
        self,
    ) -> None:
        rng = Random(SEED)
        robin = archer(arrows=1)
        target = fighter("Target", team="monsters", max_hp=200, position=100)
        encounter = Encounter([robin, target], rng)
        advance_to(encounter, "Robin", rng)

        run_encounter(encounter, rng, max_rounds=20)

        melee_swings = [
            event
            for event in encounter.log
            if event.kind == "attack" and event.data.get("attack") == "Dagger"
        ]
        assert melee_swings, (
            "the archer never drew the dagger after its one arrow was spent"
        )
        assert robin.position != (0, 0), "the archer never moved toward its target"

    def test_a_dry_ranged_option_is_not_proposed_again(self) -> None:
        rng = Random(SEED)
        robin = archer(arrows=1, position=0)
        target = fighter("Target", team="monsters", max_hp=200, position=100)
        encounter = Encounter([robin, target], rng)
        advance_to(encounter, "Robin", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Target"), FixedRandom(20))
        assert robin.items["Arrow"] == 0

        options = _attack_options(
            encounter, robin, [target], encounter.state()["turn_state"]
        )

        assert all(option.action.attack != "Shortbow" for option in options)


class TestLoadingAwarePolicy:
    """A Loading weapon already fired this turn must not be proposed a second time."""

    def crossbow(self) -> AttackOption:
        return AttackOption(
            name="Hand Crossbow",
            attack_bonus=5,
            damage=Dice(1, 6, 3),
            damage_type=DamageType.PIERCING,
            kind=AttackKind.RANGED,
            normal_range=30,
            long_range=120,
            loading=True,
            provenance=FIXTURE,
        )

    def test_a_loading_weapon_already_fired_this_turn_is_not_reproposed(self) -> None:
        rng = Random(SEED)
        shooter = fighter("Vex", position=0)
        shooter.attacks = (self.crossbow(),)
        target = fighter("Target", team="monsters", position=20)
        encounter = Encounter([shooter, target], rng)
        advance_to(encounter, "Vex", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Target"), FixedRandom(20))
        turn = encounter.state()["turn_state"]
        assert turn["loading_used"] is True

        options = _attack_options(encounter, shooter, [target], turn)

        assert options == []


class TestItemsSpentExcludesAmmunition:
    """``items_spent`` is a resource-consumption metric, and arrows are not the
    resource it was built to report: a shot fired is not a decision the way
    quaffing a potion is, and 20 arrows would swamp the one potion beside them.
    Ammunition gets its own metric, ``ammunition_spent``, derived from the
    combatants' own attacks rather than a hardcoded name.
    """

    def test_items_spent_counts_potions_not_arrows(self) -> None:
        def combatants() -> list[Creature]:
            robin = archer(arrows=20, max_hp=6)
            robin.items = {"Arrow": 20, "Potion": 1}
            foe = fighter("Goblin", team="monsters", position=15, max_hp=500)
            return [robin, foe]

        result = simulate_rounds(
            combatants,
            iterations=10,
            seed=SEED,
            max_rounds=10,
            items={"Potion": ItemEffect(heal=Dice(2, 4, 2), provenance=FIXTURE)},
        )

        party = result["teams"]["party"]
        # At most the one potion the archer carried — the bug summed every
        # arrow fired into the same figure.
        assert party["items_spent"]["max"] <= 1
        assert party["ammunition_spent"]["max"] >= 1


class TestSummarise:
    def test_empty_input_is_all_zeroes(self) -> None:
        stats = summarise([])
        assert stats.samples == 0
        assert stats.mean == 0.0

    def test_percentiles_come_from_the_sorted_sample(self) -> None:
        stats = summarise([float(value) for value in range(1, 11)])
        assert stats.samples == 10
        assert stats.minimum == 1.0
        assert stats.maximum == 10.0
        assert stats.median == 5.5
        # Nearest-rank: index round(0.9 * 9) == 8, so the ninth of ten values.
        assert stats.p90 == 9.0


def blaster(
    name: str = "Ilva", *, position: int | tuple[int, int] = 0, team: str = "party"
) -> Creature:
    """A caster carrying a weapon — the case the old policy measured wrongly."""
    return Creature(
        name=name,
        team=team,
        ac=12,
        max_hp=32,
        speed=30,
        abilities={Ability.DEXTERITY: 14, Ability.CONSTITUTION: 14},
        attacks=(
            AttackOption(
                name="Dagger",
                attack_bonus=6,
                damage=Dice.parse("1d4+4"),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.MELEE,
                provenance="synthetic test fixture, not SRD content",
            ),
        ),
        spells=("Fireball",),
        spell_slots={3: 3},
        spell_save_dc=15,
        spell_attack_bonus=7,
        position=position,
        provenance="synthetic test fixture, not SRD content",
    )


def clustered_goblins() -> Sequence[Creature]:
    """Four goblins packed inside one Fireball, and a wizard well clear of it."""
    return [
        blaster(position=0),
        *(
            make_monster("Goblin Warrior", label=f"Goblin {letter}", position=100 + step)
            for step, letter in enumerate("ABCD")
        ),
    ]


class TestPolicyChoosesByExpectedDamage:
    def test_a_caster_holding_a_weapon_still_casts(self) -> None:
        # The policy used to take attacks[0] whenever it was in range, so a caster
        # with any weapon at all never reached the spell branch. simulate_dpr — the
        # tool for "is this build better?" — understated such a build sixfold.
        armed = simulate_dpr(
            lambda: blaster(), target_ac=15, rounds=3, iterations=300, seed=SEED,
            spellbook=spellbook(),
        )
        unarmed = simulate_dpr(
            lambda: Creature(
                name="Ilva",
                team="party",
                ac=12,
                max_hp=32,
                speed=30,
                spells=("Fireball",),
                spell_slots={3: 3},
                spell_save_dc=15,
                position=0,
                provenance="synthetic test fixture, not SRD content",
            ),
            target_ac=15,
            rounds=3,
            iterations=300,
            seed=SEED,
            spellbook=spellbook(),
        )
        assert "cast:Fireball" in armed["actions"]
        # The dagger is strictly worse than the spell, so carrying one must not
        # change what the build is measured at.
        assert armed["damage"]["mean"] == pytest.approx(unarmed["damage"]["mean"])

    def test_a_weapon_wins_when_it_is_actually_the_better_option(self) -> None:
        # The converse, so the test above is not just "always prefer the spell": a
        # build whose spell slots are gone falls back to the weapon.
        def spent() -> Creature:
            creature = blaster()
            creature.spell_slots = {3: 0}
            return creature

        result = simulate_dpr(
            lambda: spent(), target_ac=15, rounds=3, iterations=200, seed=SEED,
            spellbook=spellbook(),
        )
        assert result["actions"] == {"attack:Dagger": 600}
        assert result["damage"]["mean"] > 0


class TestPolicyValuesSpellAttacks:
    """The policy weighs a spell attack under the state it will actually roll under.

    The weapon branch already asked the encounter for both Advantage and forced
    criticals; the attack-roll spell branch asked for neither, so a Guiding Bolt
    at a helpless target was valued as a flat d20 while the stepper would roll it
    with Advantage and turn every hit into a critical. CLAUDE.md: analytics
    replays the stepper, it does not keep a second copy of the rules.
    """

    def bolt_caster(self) -> Creature:
        wren = caster(position=0)
        wren.spells = ("Guiding Bolt",)
        wren.spell_slots = {1: 4}
        wren.spell_attack_bonus = 5
        return wren

    def valued(self, *, conditions: Sequence[str], distance: int) -> float:
        wren = self.bolt_caster()
        mark = Creature(
            name="Mark",
            team="foes",
            ac=15,
            max_hp=200,
            speed=30,
            position=distance,
            provenance="synthetic test fixture, not SRD content",
        )
        for condition in conditions:
            mark.add_condition(condition)
        encounter = Encounter([wren, mark], Random(SEED), spellbook=spellbook())
        options = _spell_options(encounter, wren, [mark])
        return next(
            option.value
            for option in options
            if option.tiebreak == "cast:Guiding Bolt:1:Mark"
        )

    def test_a_helpless_adjacent_target_is_valued_with_advantage_and_the_critical(
        self,
    ) -> None:
        # The oracle is the engine's own exact arithmetic under the state the
        # stepper will roll under, so the two cannot drift.
        expected = attack_damage_expectation(
            attack_bonus=5,
            target_ac=15,
            damage=Dice(4, 6),
            advantage=Advantage.ADVANTAGE,
            forced_critical=True,
        )
        assert self.valued(
            conditions=(Condition.PARALYZED,), distance=5
        ) == pytest.approx(expected)

    def test_the_same_target_out_of_reach_keeps_the_advantage_and_loses_the_critical(
        self,
    ) -> None:
        expected = attack_damage_expectation(
            attack_bonus=5,
            target_ac=15,
            damage=Dice(4, 6),
            advantage=Advantage.ADVANTAGE,
            forced_critical=False,
        )
        assert self.valued(
            conditions=(Condition.PARALYZED,), distance=30
        ) == pytest.approx(expected)

    def test_an_unhindered_target_is_still_valued_as_a_flat_roll(self) -> None:
        expected = attack_damage_expectation(
            attack_bonus=5, target_ac=15, damage=Dice(4, 6)
        )
        assert self.valued(conditions=(), distance=30) == pytest.approx(expected)


class TestPolicyValuesAttacksUnderAConditionPenalty:
    """The auto-play policy's expectations are what the stepper will actually
    roll under (CLAUDE.md, ``montecarlo.py:407-410``): a weary attacker's own
    D20 Test penalty must reach ``_attack_options`` and ``_spell_options``
    exactly as it reaches the live stepper's ``attack_bonus``/
    ``spell_attack_bonus``, or the policy would value an attack it holds by a
    number the roll it actually makes cannot produce.
    """

    TABLE = dict(EFFECTS) | {
        "weary": ConditionEffect(d20_test_penalty_per_level=2, cumulative=True),
    }

    def test_a_weary_attackers_weapon_option_is_valued_under_the_penalty(
        self,
    ) -> None:
        robin = fighter("Robin")
        robin.condition_effects = self.TABLE
        robin.add_condition("weary", levels=2)
        target = fighter("Target", team="monsters", position=5)
        encounter = Encounter([robin, target], Random(SEED), condition_effects=self.TABLE)

        options = _attack_options(
            encounter, robin, [target], encounter.state()["turn_state"]
        )
        option = next(o for o in options if o.action.attack == "Longsword")

        # ``fighter()``'s one attack: attack_bonus 5, Dice(1, 8, 3) damage.
        expected = attack_damage_expectation(
            attack_bonus=robin.attack_modifier(5),
            target_ac=target.ac,
            damage=Dice(1, 8, 3),
        )
        assert option.value == pytest.approx(expected)
        # Without the penalty this would value higher — the check that the
        # penalty was actually applied, not merely that some value exists.
        unmodified = attack_damage_expectation(
            attack_bonus=5, target_ac=target.ac, damage=Dice(1, 8, 3)
        )
        assert option.value < unmodified

    def test_a_weary_casters_attack_spell_option_is_valued_under_the_penalty(
        self,
    ) -> None:
        wren = caster(position=0)
        wren.spells = ("Guiding Bolt",)
        wren.spell_slots = {1: 4}
        wren.spell_attack_bonus = 5
        wren.condition_effects = self.TABLE
        wren.add_condition("weary", levels=2)
        mark = Creature(
            name="Mark", team="foes", ac=15, max_hp=200, speed=30, position=30,
            provenance="synthetic test fixture, not SRD content",
        )
        encounter = Encounter(
            [wren, mark], Random(SEED), spellbook=spellbook(), condition_effects=self.TABLE
        )

        options = _spell_options(encounter, wren, [mark])
        value = next(
            option.value
            for option in options
            if option.tiebreak == "cast:Guiding Bolt:1:Mark"
        )

        expected = attack_damage_expectation(
            attack_bonus=1,  # spell_attack_bonus 5 - 4 penalty
            target_ac=15,
            damage=Dice(4, 6),
        )
        assert value == pytest.approx(expected)


class TestPolicyPlacesAreaSpells:
    def test_a_fireball_is_placed_to_catch_the_whole_cluster(self) -> None:
        # The decision itself is what matters here, so it is read straight off the
        # policy rather than inferred from the event log. The old policy always
        # passed a single name and never a point of origin, so a 20 ft blast landed
        # on exactly one goblin however tightly they were packed.
        rng = Random(SEED)
        combatants = list(clustered_goblins())
        encounter = Encounter(combatants, rng, spellbook=spellbook())
        advance_to(encounter, "Ilva", rng)
        action = auto_action(encounter)
        assert action is not None
        assert action.kind is ActionKind.CAST
        assert action.spell == "Fireball"
        assert action.center is not None
        # The caught set is the assertion, not the centre coordinate: any placement
        # that blankets the cluster and spares the caster is a correct answer.
        centre = as_point(action.center)
        caught = [
            creature
            for creature in combatants
            if distance_feet(as_point(creature.position), centre) <= 20
        ]
        assert sorted(c.name for c in caught) == [
            "Goblin A", "Goblin B", "Goblin C", "Goblin D",
        ]

    def test_the_caster_is_never_caught_in_its_own_blast(self) -> None:
        # A wizard standing among the goblins must not centre a Fireball on itself.
        rng = Random(SEED)
        wizard = blaster(position=102)
        goblins = [
            make_monster("Goblin Warrior", label=f"Goblin {letter}", position=100 + step)
            for step, letter in enumerate("ABCD")
        ]
        encounter = Encounter([wizard, *goblins], rng, spellbook=spellbook())
        run_encounter(encounter, rng, max_rounds=20)
        hit_self = [
            event
            for event in encounter.log
            if event.kind == "spell_effect" and event.target == wizard.name
        ]
        assert hit_self == []

    def test_placement_beats_naming_a_single_target(self) -> None:
        # The measurable consequence of the fix: against a cluster, the same party
        # at the same seed wins far more often than it did casting single-target.
        result = simulate_rounds(
            clustered_goblins, iterations=200, seed=SEED, max_rounds=20,
            spellbook=spellbook(),
        )
        assert result["win_rate"].get("party", 0.0) > 0.5


FIXTURE = "synthetic test fixture, not SRD content"


def walled_arena() -> BattleMap:
    """A 6x3 arena with a wall stub: melee must walk around, sight is partial."""
    return BattleMap.flat(
        name="arena", width=6, height=3,
        terrain={(2, 0): "wall", (2, 1): "wall"},
        provenance=FIXTURE,
    )


def mapped_duel() -> Sequence[Creature]:
    return [
        fighter("Thora", position=(0, 0)),
        make_monster("Goblin Warrior", label="Goblin", position=(25, 0)),
    ]


class TestMappedAnalytics:
    def test_a_mapped_iteration_matches_a_single_hand_driven_run(self) -> None:
        # The mapped sibling of the load-bearing parity test: one batch iteration
        # on a map must equal a hand-driven encounter on the identical map.
        rng = Random(SEED)
        encounter = Encounter(
            list(mapped_duel()), rng, spellbook=spellbook(),
            battle_map=walled_arena(),
        )
        manual = run_encounter(encounter, rng, max_rounds=20)

        batch = simulate_rounds(
            mapped_duel, iterations=1, seed=SEED, max_rounds=20,
            spellbook=spellbook(), battle_map=walled_arena(),
        )

        expected_winner = manual.winner if manual.winner is not None else "none"
        assert batch["wins"] == {expected_winner: 1}
        assert batch["rounds"]["mean"] == float(manual.rounds)

    def test_the_same_seed_produces_an_identical_mapped_transcript(self) -> None:
        def transcript(seed: int) -> list[tuple[str, str, str, str]]:
            rng = Random(seed)
            encounter = Encounter(
                list(mapped_duel()), rng, spellbook=spellbook(),
                battle_map=walled_arena(),
            )
            run_encounter(encounter, rng, max_rounds=20)
            return [
                (event.kind, event.actor, event.target, event.detail)
                for event in encounter.log
            ]

        assert transcript(SEED) == transcript(SEED)

    def test_the_policy_closes_around_the_wall_and_the_fight_concludes(self) -> None:
        # A dominant-axis stepper would march into the wall, be refused, and
        # stall to the round cap; a routed closer reaches the goblin and ends it.
        result = simulate_rounds(
            mapped_duel, iterations=5, seed=SEED, max_rounds=20,
            spellbook=spellbook(), battle_map=walled_arena(),
        )
        assert result["timed_out"] == 0
        assert sum(result["wins"].values()) == 5


class TestPolicyPlacesShapes:
    def test_a_cone_is_aimed_at_the_wedge_that_catches_the_cluster(self) -> None:
        rng = Random(SEED)
        combatants = [
            shaper(position=(0, 0)),
            make_monster("Goblin Warrior", label="East A", position=(10, 0)),
            make_monster("Goblin Warrior", label="East B", position=(10, 5)),
        ]
        encounter = Encounter(combatants, rng, spellbook=shaped_spellbook())
        advance_to(encounter, "Vesna", rng)
        action = auto_action(encounter)
        assert action is not None
        assert action.kind is ActionKind.CAST
        assert action.spell == "Flame Fan"
        assert action.direction is not None
        caught = encounter.area_targets(
            encounter.spellbook["Flame Fan"], "Vesna", direction=action.direction
        )
        assert sorted(creature.name for creature in caught) == ["East A", "East B"]

    def test_a_line_is_aimed_down_the_rank_of_enemies(self) -> None:
        rng = Random(SEED)
        combatants = [
            shaper(position=(0, 0)),
            make_monster("Goblin Warrior", label="Near", position=(15, 0)),
            make_monster("Goblin Warrior", label="Far", position=(30, 0)),
        ]
        encounter = Encounter(combatants, rng, spellbook=shaped_spellbook())
        # Keep only the line in hand so the choice under test is the aim.
        encounter.creatures["Vesna"].spells = ("Spark Line",)
        advance_to(encounter, "Vesna", rng)
        action = auto_action(encounter)
        assert action is not None
        assert action.spell == "Spark Line"
        assert action.toward is not None
        caught = encounter.area_targets(
            encounter.spellbook["Spark Line"], "Vesna", toward=action.toward
        )
        assert sorted(creature.name for creature in caught) == ["Far", "Near"]

    def test_a_cube_corner_is_chosen_to_cover_both_targets(self) -> None:
        rng = Random(SEED)
        combatants = [
            shaper(position=(0, 0)),
            make_monster("Goblin Warrior", label="A", position=(30, 0)),
            make_monster("Goblin Warrior", label="B", position=(35, 5)),
        ]
        encounter = Encounter(combatants, rng, spellbook=shaped_spellbook())
        encounter.creatures["Vesna"].spells = ("Stone Cube",)
        advance_to(encounter, "Vesna", rng)
        action = auto_action(encounter)
        assert action is not None
        assert action.spell == "Stone Cube"
        assert action.center is not None
        caught = encounter.area_targets(
            encounter.spellbook["Stone Cube"], "Vesna", center=action.center
        )
        assert sorted(creature.name for creature in caught) == ["A", "B"]

    def test_a_sphere_on_a_map_is_never_centred_where_the_caster_cannot_see(
        self,
    ) -> None:
        # Goblins far enough behind a full-height wall that every origin within
        # the blast's radius of them is on the hidden side: no visible origin
        # catches them, so the policy must not propose the cast at all. Everyone
        # stands in interior rows — a corner on the map boundary could legally
        # graze along the edge, which is the sight policy, not the subject here.
        def propose(terrain: dict[tuple[int, int], str]) -> Any:
            rng = Random(SEED)
            battle_map = BattleMap.flat(
                name="sealed", width=10, height=5, terrain=terrain, provenance=FIXTURE,
            )
            combatants = [
                blaster(position=(0, 10)),
                make_monster("Goblin Warrior", label="Hidden A", position=(40, 10)),
                make_monster("Goblin Warrior", label="Hidden B", position=(40, 15)),
            ]
            encounter = Encounter(
                combatants, rng, spellbook=spellbook(), battle_map=battle_map
            )
            advance_to(encounter, "Ilva", rng)
            return auto_action(encounter)

        # Sealed off, the policy declines to cast.
        assert propose({(3, row): "wall" for row in range(5)}) is None

        # The positive control, and the reason the None above means anything: the
        # same geometry with the wall removed *does* propose the cast. Without it
        # this test would pass against an auto_action() that returned None always.
        unsealed = propose({})
        assert unsealed is not None
        assert unsealed.kind is ActionKind.CAST


class TestRoundsReported:
    def test_a_timed_out_fight_reports_the_cap_not_one_past_it(self) -> None:
        # advance() ticks the round over before the loop guard fails, so this used
        # to report 21 rounds for a 20-round cap.
        def stalemate() -> Sequence[Creature]:
            # Two creatures that cannot reach each other and have no ranged option.
            return [
                fighter("Thora", position=0),
                fighter("Ulf", team="foes", position=10_000),
            ]

        result = simulate_rounds(stalemate, iterations=3, seed=SEED, max_rounds=5)
        assert result["timed_out"] == 3
        assert result["rounds"]["max"] == 5.0


class TestPolicyLeavesDownedCreaturesAlone:
    """The stepper now permits a finishing blow; the policy still declines to take one.

    That split is deliberate. Whether a downed creature *can* be hit is a rules
    question and the answer is yes. Whether an auto-played combatant *should* spend
    its action doing so is a tactical one the SRD does not answer, and a greedy
    one-turn policy is not the place to decide it: a downed creature threatens
    nobody, so finishing it costs a turn that a batch's win-rate arithmetic would
    have spent on a standing enemy.

    These tests pin the policy against the stepper's new permission. They are the
    reason the batch numbers this engine already published stay comparable.
    """

    @staticmethod
    def _brawl_with_one_enemy_down() -> tuple[Encounter, Creature]:
        """Thora against two goblins, one of them dropped on the spot.

        The goblin is dropped *after* the encounter reaches Thora's turn, so no
        death save is rolled in between — one natural 20 would put it back on its
        feet and quietly empty the test.
        """
        rng = Random(SEED)
        thora = fighter("Thora", position=0)
        downed = make_monster("Goblin Warrior", label="Downed", team="foes", position=5)
        downed.death_rule = DeathRule.DEATH_SAVES
        upright = make_monster(
            "Goblin Warrior", label="Upright", team="foes", position=10
        )
        encounter = Encounter([thora, downed, upright], rng, spellbook=spellbook())
        advance_to(encounter, "Thora", rng)
        downed.take_damage(downed.hp)
        assert downed.dying
        return encounter, downed

    def test_a_downed_enemy_is_never_named_by_a_chosen_action(self) -> None:
        encounter, downed = self._brawl_with_one_enemy_down()
        assert downed.name not in [c.name for c in encounter.enemies_of("Thora")]
        for _ in range(MAX_ACTIONS_PER_TURN):
            action = auto_action(encounter)
            if action is None:
                break
            assert action.target != downed.name
            assert downed.name not in action.targets
            encounter.act(action, Random(2))

    def test_a_side_that_is_only_dying_draws_no_action_at_all(self) -> None:
        # Not merely "no attack on the downed one": with nothing conscious left to
        # hit, the policy stops rather than falling through to a finishing blow.
        rng = Random(SEED)
        thora = fighter("Thora", position=0)
        ally = fighter("Bern", position=5)  # keeps ``over`` from firing on the team
        foe = make_monster("Goblin Warrior", label="Goblin", team="foes", position=10)
        foe.death_rule = DeathRule.DEATH_SAVES
        encounter = Encounter([thora, ally, foe], rng, spellbook=spellbook())
        advance_to(encounter, "Thora", rng)
        foe.take_damage(foe.hp)
        assert foe.dying
        assert auto_action(encounter) is None

    def test_an_area_spell_is_placed_on_the_standing_enemy_not_the_downed_one(
        self,
    ) -> None:
        # The placement search enumerates candidate origins from conscious creatures
        # only, so the downed goblin at the wizard's feet never becomes a point of
        # origin even though the stepper would now resolve a blast there.
        rng = Random(SEED)
        wizard = blaster(position=50)
        downed = make_monster("Goblin Warrior", label="Downed", team="foes", position=5)
        upright = make_monster(
            "Goblin Warrior", label="Upright", team="foes", position=100
        )
        encounter = Encounter([wizard, downed, upright], rng, spellbook=spellbook())
        advance_to(encounter, "Ilva", rng)
        downed.take_damage(downed.hp)

        action = auto_action(encounter)
        assert action is not None
        assert action.kind is ActionKind.CAST
        assert action.center is not None
        assert distance_feet(as_point(action.center), as_point(upright.position)) <= 20
        assert distance_feet(as_point(action.center), as_point(downed.position)) > 20

    def test_the_policy_still_declines_over_a_whole_batch(self) -> None:
        # The end-to-end guard: across many auto-played fights, no attack or spell
        # effect is ever logged against a creature that was down when it landed. A
        # per-turn assertion cannot see a Multiattack's later swings.
        #
        # ``down``, ``death`` and ``death_save`` all carry the creature in ``actor``;
        # ``attack`` and ``spell_effect`` carry it in ``target``. A natural 20 on a
        # death save puts the creature back up, so it leaves the down set again —
        # without that, a legitimate later attack would read as a violation.
        for index in range(40):
            rng = Random(SEED + index)
            encounter = Encounter(list(melee_brawl()), rng, spellbook=spellbook())
            run_encounter(encounter, rng, max_rounds=20)
            down: set[str] = set()
            for event in encounter.log:
                if event.kind == "down":
                    down.add(event.actor)
                elif event.kind == "death_save" and "regains" in event.detail:
                    down.discard(event.actor)
                elif event.kind in {"attack", "opportunity_attack", "spell_effect"}:
                    assert event.target not in down, (
                        f"iteration {index}: {event.kind} on {event.target} "
                        f"while it was down ({event.detail})"
                    )
